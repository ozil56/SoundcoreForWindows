"""D3200 SoftAP + WSS download, retaining the existing verified WAV exporter.

Based on PROTOCOL.md 6.4–6.5 and lib/wifi/wifi_export_service.dart.
No delete, binding, firmware, or recording-control commands are issued.
"""
import asyncio
import contextlib
import ipaddress
import secrets
import socket
import ssl
import struct
import time

import websocket

from recorder_files import RecordingStarted, WavExport, check_status
import soundcore_stream as protocol
from windows_wifi import TemporaryWifi


def wifi_config(ssid, password):
    name, secret = ssid.encode('utf-8'), password.encode('utf-8')
    if not 1 <= len(name) <= 32 or not 8 <= len(secret) <= 63:
        raise ValueError('Invalid temporary hotspot credentials')
    return bytes([len(name)]) + name + bytes([len(secret)]) + secret


def endpoint_from(packet):
    check_status(packet)
    if packet[5:7] != b'\x1a\x05' or len(packet) < 16:
        raise ValueError('Invalid SoftAP endpoint response')
    address = ipaddress.IPv4Address(packet[9:13][::-1])
    port = struct.unpack_from('<H', packet, 13)[0]
    if not address.is_private or address.is_unspecified or address.is_loopback or not port:
        raise ValueError('Recorder returned an invalid local endpoint')
    return str(address), port


def file_request(file_id):
    # SDK Wi-Fi requests are 18 bytes, with no trailing checksum.
    return (b'\x08\xee\0\0\0\x1a\x07\x12\0' + struct.pack('<II', 0, file_id) + b'\x1b').hex()


class PacketBuffer:
    def __init__(self):
        self.data = bytearray()

    def feed(self, data):
        if isinstance(data, str):
            if data.strip().upper() in ('ACK', 'OK', 'PONG'):
                return []
            try:
                data = bytes.fromhex(data)
            except ValueError:
                raise ValueError('Unexpected Wi-Fi text response') from None
        self.data.extend(data)
        packets = []
        while len(self.data) >= 9:
            if self.data[:4] != b'\x09\xff\0\0':
                raise ValueError('Invalid Wi-Fi protocol frame prefix')
            size = int.from_bytes(self.data[7:9], 'little')
            if not 10 <= size <= 65535:
                raise ValueError('Invalid Wi-Fi protocol frame length')
            if len(self.data) < size:
                break
            packet = bytes(self.data[:size])
            del self.data[:size]
            if sum(packet[:-1]) & 255 != packet[-1]:
                raise ValueError('Wi-Fi protocol checksum mismatch')
            packets.append(packet)
        return packets


class WifiTransfer:
    def __init__(self, session, report=None, progress=None):
        self.session = session
        self.report = report if report is not None else {}
        self.progress = progress or protocol.log
        self.wifi = self.ws = self.keepalive = None
        self.opened = False
        self.ble_detached = False
        self.endpoint = None
        self.credentials = None
        self.configured_at = 0
        self.buffer = PacketBuffer()

    async def __aenter__(self):
        try:
            self.session.check_idle()
            self.wifi = TemporaryWifi()
            self.report['wifi_before'] = self.wifi.original
            files = await self.session.list_files()
            if self.session.is_recording() or any(item.active for item in files):
                raise RecordingStarted('录音豆正在录音，请停止后再进行 Wi-Fi 快传')
            info = await self.session.request(1, 1)
            check_status(info)
            self.session.check_idle()
            if self.session.is_recording() or (len(info) > 59 and info[59] == 1):
                raise RecordingStarted('设备开始录音，已取消开启热点')
            raw_battery = info[10]
            battery = (raw_battery + 1) * 10 if raw_battery <= 9 else raw_battery
            self.report['battery_percent'] = battery
            if battery < 30:
                raise RuntimeError(f'录音豆电量 {battery}%，请充至至少 30% 再进行快传')
            ssid = 'WiFi-' + str(int(time.time()*1000))
            password = secrets.token_hex(8)
            self.credentials = (ssid, password)
            self.progress('正在通过蓝牙开启录音豆临时热点')
            self.opened = True  # Also clean up if the response is lost.
            await self.session.send(0x1A, 5, wifi_config(ssid, password))
            response = await self.session.receive(0x1A, 5, timeout=45)
            ip, port = endpoint_from(response)
            self.endpoint = (ip, port)
            self.configured_at = time.monotonic()
            self.report['endpoint'] = {'ssid': ssid, 'ip': ip, 'port': port}
            protocol.log(f'SoftAP endpoint {ip}:{port}')
            self.progress('录音豆热点启动中…')
            # Keep the ECDH key from this BLE session for the Wi-Fi file headers.
            self.ble_detached = True
            await self.session.client.disconnect()
            await asyncio.sleep(10)
            self.session.check_idle()
            self.report['wifi_connection'] = await self.wifi.join(ssid, password, ip)
            self.session.check_idle()
            self.progress('已连接录音豆热点，正在准备下载…')
            self.keepalive = asyncio.create_task(self._keepalive(ip))
            last_error = None
            for attempt in range(3):
                try:
                    await self._connect_socket()
                    self.report['wss_connected'] = True
                    self.progress('正在通过 Wi-Fi 快传下载录音…')
                    return self
                except Exception as error:
                    last_error = error
                    protocol.log(f'WebSocket attempt {attempt+1}: {type(error).__name__}: {error}')
                    await asyncio.sleep(2)
            raise ConnectionError(f'无法连接录音豆 WebSocket：{last_error}')
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def _connect_socket(self):
        ip, port = self.endpoint
        # Only the endpoint returned by this selected recorder's BLE session.
        connecting = asyncio.create_task(asyncio.to_thread(websocket.create_connection,
            f'wss://{ip}:{port}/', timeout=10,
            sslopt={'cert_reqs': ssl.CERT_NONE, 'check_hostname': False},
            suppress_origin=True, http_no_proxy=['*'], host=f'{ip}:{port}',
            header={'Accept-Encoding': 'gzip', 'User-Agent': 'okhttp/3.12.13.18'}))
        try:
            self.ws = await asyncio.shield(connecting)
        except asyncio.CancelledError:
            # Reclaim the bounded worker's socket before restoring the network.
            with contextlib.suppress(Exception):
                self.ws = await connecting
            raise
        self.ws.settimeout(1)

    async def _keepalive(self, ip):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind((self.report['wifi_connection']['local_ip'], 0))
            while True:
                udp.sendto(b'soundcore-keep-alive-unicast', (ip, 32003))
                await asyncio.sleep(2)

    async def export(self, recording, folder, progress=lambda *_: None):
        self.session.check_idle()
        if self.buffer.data:
            raise ValueError('上一文件仍有未完成的 Wi-Fi 数据包')
        writer = WavExport(folder, self.session.identity.hex(), recording, self.session.session_key)
        started, last_progress = time.monotonic(), 0
        deadline = started + 30
        try:
            await asyncio.to_thread(self.ws.send, file_request(recording.file_id))
            while not writer.done:
                self.session.check_idle()
                if time.monotonic() > deadline:
                    raise TimeoutError('等待 Wi-Fi 文件数据超时')
                if self.keepalive and self.keepalive.done():
                    self.keepalive.result()
                receiving = asyncio.create_task(asyncio.to_thread(self.ws.recv))
                try:
                    data = await asyncio.shield(receiving)
                except websocket.WebSocketTimeoutException:
                    continue
                except asyncio.CancelledError:
                    # recv has a one-second socket timeout. Let that bounded read
                    # finish so FINISH can still be sent on the existing socket.
                    with contextlib.suppress(Exception):
                        await receiving
                    raise
                self.session.check_idle()
                if data in ('', b''):
                    raise ConnectionError('录音豆提前关闭了文件通道')
                for packet in self.buffer.feed(data):
                    if ((packet[5:7] == b'\x1a\x06' and len(packet) >= 14 and struct.unpack_from('<I', packet, 9)[0])
                            or (packet[5:7] == b'\x18\x82' and len(packet) > 10 and packet[9] == 1)):
                        raise RecordingStarted('设备开始录音，Wi-Fi 下载已中止，原录音保留')
                    if packet[6] not in (8, 0x12):
                        protocol.log(f'Wi-Fi RX {packet[5]:02X}/{packet[6]:02X} bytes={len(packet)}')
                    if writer.feed(packet):
                        deadline = time.monotonic()+30
                if time.monotonic() - last_progress >= 2:
                    protocol.log(f'Wi-Fi received {writer.frames*160}/{writer.expected}')
                    last_progress = time.monotonic()
                progress(writer.frames*166 if writer.expected == writer.frames*166 else writer.frames*160, writer.expected)
            self.session.check_idle()
            result = writer.finish()
            elapsed = time.monotonic() - started
            result.update(transport='wifi', transfer_seconds=round(elapsed, 3),
                          encoded_kib_per_second=round(writer.frames*160/1024/elapsed, 2))
            return result
        finally:
            writer.close()

    async def __aexit__(self, *_):
        cleanup = asyncio.create_task(self._cleanup())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def _cleanup(self):
        cleanup_errors = []
        recovery_errors = []
        if self.ws is not None and not getattr(self.ws, 'connected', True):
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.ws.close, timeout=1)
            self.ws = None
        if self.ble_detached and self.ws is None and self.endpoint and self.credentials:
            # Firmware can suspend BLE while SoftAP is starting. On early cancel
            # finish the WLAN/WSS handoff just far enough to send FINISH; never
            # request audio. A blind BLE reconnect here can stall until AP expiry.
            self.progress('正在结束热点会话并恢复原连接…')
            try:
                await asyncio.sleep(max(0, 10-(time.monotonic()-self.configured_at)))
                ip, _ = self.endpoint
                ssid, password = self.credentials
                current = self.wifi.current()
                if not current.get('connected') or current.get('ssid') != ssid:
                    await self.wifi.join(ssid, password, ip, timeout=15)
                await self._connect_socket()
                self.report['cleanup_wss_connected'] = True
            except Exception as error:
                cleanup_errors.append(f'结束启动中的热点会话：{type(error).__name__}: {error}')
        if self.ws:
            try:
                self.ws.settimeout(2)
                await asyncio.to_thread(self.ws.send, 'FINISH')
                await asyncio.sleep(.2)
                await asyncio.to_thread(self.ws.send, protocol.frame(0x1A, 0x0A, b'\1').hex())
            except Exception as error:
                cleanup_errors.append('WebSocket finish: ' + str(error))
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.ws.close, timeout=2)
            self.ws = None
        if self.keepalive:
            self.keepalive.cancel()
            try:
                await self.keepalive
            except asyncio.CancelledError:
                pass
            except Exception as error:
                cleanup_errors.append('Keepalive: ' + str(error))
            self.keepalive = None
        if self.opened:
            try:
                if self.ble_detached:
                    await self.session.restore_after_wifi()
                else:
                    await self.session.send(0x1A, 2)
                    check_status(await self.session.receive(0x1A, 2, timeout=5))
                self.report['close_hotspot_command_sent'] = True
                self.report['ble_restored'] = True
            except Exception as error:
                recovery_errors.append(f'关闭热点/恢复蓝牙：{type(error).__name__}: {error}')
            self.opened = False
        if self.wifi:
            try:
                self.report['wifi_cleanup'] = await self.wifi.restore()
            except Exception as error:
                recovery_errors.append('恢复原 Wi-Fi：' + str(error))
            finally:
                self.wifi.close()
                self.wifi = None
        self.report['cleanup_errors'] = cleanup_errors
        self.report['recovery_errors'] = recovery_errors
        self.credentials = None

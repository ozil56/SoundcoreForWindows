"""D3200 discovery and read-only recording export, using tacshi/Soundcore protocol.

No account login, binding, erase, firmware or recorder-control commands are sent.
"""
import asyncio
from dataclasses import asdict, dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import struct
import time
import uuid
import wave

from bleak import BleakClient, BleakScanner
import soundcore_stream as protocol
from tool_settings import read_settings, update_settings, write_json


class RecordingStarted(Exception):
    """Yield a background transfer to physical recording immediately."""


@dataclass
class Candidate:
    identity_hex: str
    name: str
    rssi: int
    bound_advertised: bool


def candidate_from(device, adv):
    services = set(s.lower() for s in adv.service_uuids)
    services.update(s.lower() for s in adv.service_data)
    name = adv.local_name or device.name or ''
    # Do not classify arbitrary Soundcore headphones or arbitrary manufacturer data.
    if protocol.SERVICE not in services and not ('soundcore' in name.lower() and 'work' in name.lower()):
        return None
    identities = []
    for key, value in adv.manufacturer_data.items():
        raw = key.to_bytes(2, 'little') + value
        if len(raw) >= 8 and raw[:6] not in (bytes(6), b'\xff'*6):
            identities.append((raw[:6].hex(), bool(raw[6] & 0x80)))
    if len(identities) != 1:
        return None
    identity, bound = identities[0]
    return Candidate(identity, name or 'soundcore Work', adv.rssi, bound)


async def scan(seconds=10):
    found = {}
    def callback(device, adv):
        candidate = candidate_from(device, adv)
        if candidate:
            found[candidate.identity_hex] = candidate
    async with BleakScanner(detection_callback=callback):
        await asyncio.sleep(seconds)
    return sorted(found.values(), key=lambda item: -item.rssi)


def load_selection(root):
    try:
        data = json.loads((Path(root)/'recorder-identity.json').read_text(encoding='utf-8-sig'))
        if len(bytes.fromhex(data['identity_hex'])) != 6:
            raise ValueError('identity length')
        data['identity_hex'] = data['identity_hex'].lower()
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_selection(root, candidate):
    if len(bytes.fromhex(candidate.identity_hex)) != 6:
        raise ValueError('录音豆标识无效')
    path = Path(root)/'recorder-identity.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(asdict(candidate), ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


@dataclass
class Recording:
    file_id: int
    end_time: int
    size: int
    active: bool = False

    @property
    def title(self):
        if 946684800 <= self.file_id <= 4102444800:
            return dt.datetime.fromtimestamp(self.file_id).strftime('%Y-%m-%d %H:%M:%S')
        return str(self.file_id)


def check_status(packet):
    if len(packet) < 10 or packet[4] & 15 != 1:
        raise RuntimeError('设备拒绝了此操作；当前固件可能不支持，或连接正被其他程序占用。')


def parse_list(packet):
    check_status(packet)
    payload = packet[9:-1]
    if len(payload) < 2:
        raise ValueError('录音列表不完整')
    count = struct.unpack_from('<H', payload)[0]
    if count > 10 or len(payload) != 2 + count*12 + 8:
        raise ValueError('录音列表格式不符合 D3200 协议，未使用不完整列表')
    current, duration = struct.unpack_from('<II', payload, 2 + count*12)
    files = [Recording(*struct.unpack_from('<III', payload, 2+i*12)) for i in range(count)]
    for item in files:
        item.active = bool(current and item.file_id == current)
    return count, files, current, duration


class WavExport:
    """Transactional decode: final WAV exists only after validated completion."""
    def __init__(self, folder, identity, recording, session_key, decoder_factory=protocol.Decoder):
        self.recording = recording
        self.session_key = session_key
        self.key = self.nonce = None
        self.expected = recording.size
        self.first = self.last = None
        self.frames = self.samples = 0
        self.done = False
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.base = f'{identity}_{recording.file_id}'
        self.temporary = self.folder/f'.{self.base}.{uuid.uuid4().hex}.part.wav'
        self.decoder = decoder_factory()
        try:
            self.wave = wave.open(str(self.temporary), 'wb')
            self.wave.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        except BaseException:
            self.decoder.close()
            raise

    def feed(self, packet):
        if packet[5] not in (0x1A, 0x1B):
            return False
        cmd, payload = packet[6], packet[9:-1]
        if cmd not in (7, 8, 0x12, 0x0A):
            return False
        check_status(packet)
        if cmd == 7:
            if self.key is not None or len(packet) < 97 or packet[95] != 0:
                raise ValueError('设备返回无效文件头')
            file_id, size = struct.unpack_from('<II', packet, 9)
            if file_id != self.recording.file_id:
                raise ValueError('设备返回的文件与选择的录音不一致')
            plain = protocol.aes(self.session_key, packet[79:95], packet[33:79])
            if plain[:14] != b'soundcored3200' or len(plain) != 46:
                raise ValueError('录音密钥验证失败')
            self.key, self.nonce = plain[14:], packet[17:33]
            self.expected = size or self.expected
        elif cmd in (8, 0x12):
            if self.key is None or not payload or len(payload) % 166:
                raise ValueError('录音分片格式错误，导出已中止')
            for offset in range(0, len(payload), 166):
                sequence = struct.unpack_from('<I', payload, offset)[0]
                if self.last is not None and sequence <= self.last:
                    # A repeated BLE notification is harmless; missing frames are not.
                    continue
                if self.last is None:
                    if sequence not in (0, 1):
                        raise ValueError('录音开头缺失，请重新导出')
                    self.first = sequence
                elif sequence != self.last + 1:
                    raise ValueError('蓝牙传输丢失分片，请重新导出')
                data = protocol.aes(self.key, self.nonce[:12]+struct.pack('>I', sequence*10 & 0xffffffff),
                                    payload[offset+5:offset+165])
                pcm = self.decoder.decode(data)
                if not pcm:
                    raise ValueError('无法解码录音分片')
                self.wave.writeframesraw(pcm)
                self.samples += len(pcm)//2
                self.frames += 1
                self.last = sequence
        else:
            self.done = True
        return True

    def finish(self):
        # Firmware sizes can count 160-byte audio blocks or 166-byte transport units.
        # Require an explicit done event AND exact size, rather than upstream's 95% rule.
        if not self.done or not self.frames or self.expected not in (self.frames*160, self.frames*166):
            raise ValueError(f'录音不完整或固件长度格式未知：{self.frames} 帧 / {self.expected} 字节；未生成完成文件')
        self.wave.close()
        self.decoder.close()
        with self.temporary.open('rb+') as saved:
            saved.flush()
            os.fsync(saved.fileno())
        with wave.open(str(self.temporary), 'rb') as saved:
            if saved.getnframes() != self.samples or saved.getframerate() != 16000:
                raise ValueError('WAV 文件校验失败，设备原录音保留')
        with self.temporary.open('rb') as saved:
            digest = hashlib.file_digest(saved, 'sha256').hexdigest()
        for index in range(10000):
            suffix = '' if not index else f'_{index}'
            target = self.folder/f'{self.base}{suffix}.wav'
            try:
                # On Windows rename is atomic and fails if target already exists.
                self.temporary.rename(target)
                return {'path':str(target), 'frames':self.frames, 'seconds':self.samples/16000,
                        'expected_bytes':self.expected, 'complete':True,
                        'file_id':self.recording.file_id, 'identity_hex':self.base.split('_')[0],
                        'sha256':digest, 'bytes':target.stat().st_size,
                        'list_size':self.recording.size, 'end_time':self.recording.end_time,
                        'downloaded_at':dt.datetime.now().astimezone().isoformat()}
            except FileExistsError:
                continue
        raise RuntimeError('同名文件过多，请选择其他导出文件夹')

    def close(self):
        self.wave.close()
        self.decoder.close()
        self.temporary.unlink(missing_ok=True)


class RecorderSession(protocol.Receiver):
    @classmethod
    def attached(cls, receiver, identity):
        """Borrow the live connection on the same event loop; never disconnect it."""
        session = cls.__new__(cls)
        session.client, session.queue = receiver.client, receiver.queue
        session.identity = bytes.fromhex(identity)
        session.session_key = receiver.session_key
        generation = receiver.recording_generation
        session.recording_guard = lambda: receiver.recording_active or receiver.recording_generation != generation
        return session

    def check_idle(self):
        if getattr(self, 'transfer_guard', None):
            self.transfer_guard()
        if getattr(self, 'recording_guard', lambda: False)():
            raise RecordingStarted('设备开始录音，后台下载已让出连接')

    def __init__(self, identity):
        super().__init__(Path('.'), save_audio=False)
        self.identity = bytes.fromhex(identity)
        if len(self.identity) != 6:
            raise ValueError('设备标识无效')
        self.info = {}

    async def send(self, typ, cmd, payload=b''):
        async with asyncio.timeout(10):
            await super().send(typ, cmd, payload)

    async def __aenter__(self):
        try:
            def matches(device, adv):
                candidate = candidate_from(device, adv)
                return candidate is not None and candidate.identity_hex == self.identity.hex()
            device = await BleakScanner.find_device_by_filter(matches, timeout=20)
            if not device:
                raise RuntimeError('未找到所选录音豆。请唤醒设备，并暂停其他电脑桥接、断开手机 App 的连接。')
            self.client = BleakClient(device, timeout=20, pair=False)
            await self.client.connect()
            if not self.client.services.get_characteristic(protocol.WRITE) or not self.client.services.get_characteristic(protocol.NOTIFY):
                raise RuntimeError('所选设备不支持 D3200 读写特征')
            await self.client.start_notify(protocol.NOTIFY, self.notify)
            packet = await self.request(1, 1)
            check_status(packet)
            body = packet[9:-1]
            if len(body) < 51:
                raise RuntimeError('设备信息格式不兼容')
            self.info = {'firmware':body[3:8].decode('ascii', errors='replace'), 'recording':body[50] == 1}
            await self.handshake()
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        try:
            if self.client and self.client.is_connected:
                try:
                    async with asyncio.timeout(3):
                        await self.send(0x1A, 0x11, b'\0')
                except Exception:
                    pass
                async with asyncio.timeout(12):
                    await self.client.disconnect()
        finally:
            self.decoder.close()

    async def list_files(self):
        files, seen_pages = {}, set()
        for page in range(1000):
            packet = await self.request(0x1B, 0x0E, struct.pack('<H', page))
            count, entries, current, duration = parse_list(packet)
            fingerprint = tuple(item.file_id for item in entries)
            if count and fingerprint in seen_pages:
                raise ValueError('设备重复返回同一页录音，未将部分列表当成完整结果')
            seen_pages.add(fingerprint)
            for item in entries:
                if item.size or item.active:
                    files[item.file_id] = item
            if count < 10:
                # Some firmware lists the active recording only in the trailer.
                if current:
                    files[current] = Recording(current, 0, files[current].size if current in files else 0, True)
                return sorted(files.values(), key=lambda item: -item.file_id)
        raise ValueError('录音列表页数超过限制')

    async def export(self, recording, folder, progress=lambda *_:None):
        # Re-read before export: a stale UI selection must not export an active file.
        files = {item.file_id:item for item in await self.list_files()}
        if any(item.active for item in files.values()):
            raise ValueError('录音豆仍在录音。请弹起侧键停止录音并刷新列表，再下载历史录音。')
        selected = files.get(recording.file_id)
        if selected is None or selected.active:
            raise ValueError('此录音正在录制或已不在设备上。请先停止录音，再刷新列表。')
        writer = WavExport(folder, self.identity.hex(), selected, self.session_key)
        try:
            self.check_idle()
            protocol.log(f'Requesting completed recording {selected.file_id}, size={selected.size}')
            await self.send(0x1A, 7, struct.pack('<IIB', 0, selected.file_id, 0))
            protocol.log('Offline request sent; waiting for file header')
            deadline = time.monotonic()+20
            while not writer.done:
                self.check_idle()
                timeout = deadline-time.monotonic()
                if timeout <= 0:
                    raise TimeoutError('等待录音数据超时，未生成完成文件。请保持设备靠近并重试。')
                packet = await asyncio.wait_for(self.queue.get(), timeout)
                self.check_idle()
                # Also protect manual exports when the side button is pressed.
                if ((packet[5:7] == b'\x1a\x06' and len(packet) >= 14 and struct.unpack_from('<I',packet,9)[0])
                        or (packet[5:7] == b'\x18\x82' and len(packet)>10 and packet[9] == 1)):
                    raise RecordingStarted('设备开始录音，下载已中止，原录音保留')
                if packet[6] not in (8, 0x12):
                    protocol.log(f'File RX {packet[5]:02X}/{packet[6]:02X} status={packet[4]:02X} bytes={len(packet)-10}')
                if writer.feed(packet):
                    deadline = time.monotonic()+90
                    progress(writer.frames*160, writer.expected)
                if not self.client.is_connected:
                    raise ConnectionError('蓝牙已断开，未生成完成文件')
            self.check_idle()
            return writer.finish()
        finally:
            writer.close()
            if self.client and self.client.is_connected:
                try:
                    async with asyncio.timeout(3):
                        await self.send(0x1A, 0x11, b'\0')
                except Exception:
                    pass

    async def delete(self, file_id):
        files = {item.file_id:item for item in await self.list_files()}
        if file_id not in files:
            raise ValueError('文件已不在设备上，请刷新列表')
        if files[file_id].active:
            raise ValueError('不能删除正在录制的文件，请先停止录音')
        self.check_idle()
        response = await self.request(0x1A, 0x10, struct.pack('<I', file_id))
        check_status(response)
        # A command acknowledgement alone is not proof of successful deletion.
        if any(item.file_id == file_id for item in await self.list_files()):
            raise RuntimeError('设备尚未确认文件已删除，请刷新列表检查；本地下载会保留')


def verify_download(result):
    path = Path(result['path'])
    if not result.get('complete') or path.stat().st_size != result['bytes']:
        raise ValueError('本地文件不完整，未删除设备原录音')
    with path.open('rb') as saved:
        digest = hashlib.file_digest(saved, 'sha256').hexdigest()
    if digest != result['sha256']:
        raise ValueError('本地文件校验失败，未删除设备原录音')


async def download_one(session, recording, folder, delete_after=False, progress=lambda *_:None):
    result = await session.export(recording, folder, progress)
    # Keep a local receipt even if a later delete request fails or is cancelled.
    receipt = Path(result['path']).with_suffix('.wav.json')
    write_json(receipt, result)
    if delete_after:
        verify_download(result)
        await session.delete(recording.file_id)
        result['deleted_from_device'] = True
        write_json(receipt, result)
    return result


def download_folder(root):
    return Path(read_settings(root).get('download_dir') or Path(root)/'导出的录音')


def set_download_folder(root, folder):
    update_settings(root, download_dir=str(Path(folder).resolve()))

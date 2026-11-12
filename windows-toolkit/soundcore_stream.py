"""Windows D3200 BLE audio adapter based on tacshi/Soundcore protocol.

Protocol references: lib/ble/realtime_stream.dart, lib/crypto/device_crypto.dart,
lib/audio/opus_pcm_decoder.dart. Does not flash, erase or change device binding.
By default attaches only to the recording currently active on the device.
"""
import argparse
import asyncio
import ctypes as ct
import datetime as dt
import json
import math
from pathlib import Path
import struct
import time
import wave

from bleak import BleakClient, BleakScanner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import numpy as np
import pyogg

ROOT = Path(__file__).resolve().parent
SERVICE = '020cf5da-0000-1000-8000-00805f9b34fb'
WRITE = '00007777-0000-1000-8000-00805f9b34fb'
NOTIFY = '00008888-0000-1000-8000-00805f9b34fb'


def log(message):
    print(f'{dt.datetime.now():%H:%M:%S} {message}', flush=True)


def frame(typ, cmd, payload=b''):
    data = b'\x08\xee\x00\x00\x00' + bytes([typ, cmd]) + struct.pack('<H', len(payload)+10) + payload
    return data + bytes([sum(data) & 255])


def aes(key, iv, data):
    decrypt = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    return decrypt.update(data) + decrypt.finalize()


class Decoder:
    def __init__(self):
        self.lib = ct.CDLL(str(Path(pyogg.__file__).parent/'opus.dll'))
        self.lib.opus_decoder_create.argtypes = [ct.c_int, ct.c_int, ct.POINTER(ct.c_int)]
        self.lib.opus_decoder_create.restype = ct.c_void_p
        self.lib.opus_decode.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_int, ct.c_void_p, ct.c_int, ct.c_int]
        self.lib.opus_decode.restype = ct.c_int
        self.lib.opus_decoder_destroy.argtypes = [ct.c_void_p]
        error = ct.c_int()
        self.handle = self.lib.opus_decoder_create(16000, 1, ct.byref(error))
        if not self.handle or error.value:
            raise RuntimeError(f'Opus decoder init: {error.value}')

    def decode(self, data):
        packet = data.rstrip(b'\x00') or data[:1]
        pcm = (ct.c_int16 * 1920)()
        samples = self.lib.opus_decode(self.handle, packet, len(packet), pcm, 1920, 0)
        if samples < 0:
            raise ValueError(f'Opus decode: {samples}')
        return bytes(pcm)[:samples*2]

    def close(self):
        if self.handle:
            self.lib.opus_decoder_destroy(self.handle)
            self.handle = None


def known_identity():
    from recorder_files import load_selection
    selection = load_selection(ROOT)
    if not selection:
        raise RuntimeError('请先打开“设备与录音”，扫描并选择自己的录音豆。')
    return bytes.fromhex(selection['identity_hex'])


class Receiver:
    def __init__(self, folder, pcm_callback=None, save_audio=True):
        self.folder = folder
        self.client = None
        self.buffer = bytearray()
        self.queue = asyncio.Queue()
        self.session_key = None
        self.file_key = None
        self.nonce = None
        self.file_id = None
        self.frames_ok = self.frames_failed = self.samples = self.duplicates = self.gaps = 0
        self.first_audio = None
        self.last_audio = 0.0
        self.phase = 'scanning'
        self.last_sequence = None
        self.peak = 0
        self.last_peak = 0
        self.sum_squares = 0.0
        self.decoder = Decoder()
        self.wave = None
        self.pcm_callback = pcm_callback
        self.save_audio = save_audio
        self.stopped = False
        self.report = {'created': dt.datetime.now().astimezone().isoformat(), 'scope': 'current recording only'}

    def notify(self, _, data):
        self.buffer.extend(data)
        while len(self.buffer) >= 9:
            pos = self.buffer.find(b'\x09\xff\x00\x00')
            if pos < 0:
                del self.buffer[:-3]
                break
            if pos:
                del self.buffer[:pos]
            if len(self.buffer) < 9:
                break
            length = int.from_bytes(self.buffer[7:9], 'little')
            if length < 10 or length > 65535:
                del self.buffer[0]
                continue
            if len(self.buffer) < length:
                break
            packet = bytes(self.buffer[:length])
            del self.buffer[:length]
            if (sum(packet[:-1]) & 255) == packet[-1]:
                self.queue.put_nowait(packet)

    async def send(self, typ, cmd, payload=b''):
        await self.client.write_gatt_char(WRITE, frame(typ, cmd, payload), response=False)

    async def receive(self, typ, cmd, timeout=12):
        async with asyncio.timeout(timeout):
            while True:
                packet = await self.queue.get()
                if packet[5:7] == bytes([typ, cmd]):
                    return packet
                log(f'event {packet[5]:02X}/{packet[6]:02X} flag={packet[4]:02X} bytes={len(packet)-10}')

    async def request(self, typ, cmd, payload=b''):
        await self.send(typ, cmd, payload)
        packet = await self.receive(typ, cmd)
        log(f'RX {typ:02X}/{cmd:02X} flag={packet[4]:02X} payload={len(packet)-10}')
        return packet

    async def handshake(self):
        private = ec.generate_private_key(ec.SECP256R1())
        public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        packet = await self.request(0x2E, 1, public)
        if len(packet) < 107:
            raise RuntimeError('Device did not return complete encryption handshake')
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), packet[9:74])
        shared = private.exchange(ec.ECDH(), peer)
        if shared != packet[74:106]:
            raise RuntimeError('Encryption handshake verification failed')
        self.session_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b'\1\2\3', info=b'\1\2\3').derive(shared)
        self.report['handshake_verified'] = True
        log('Encryption handshake verified')

    def handle_audio(self, packet):
        typ, cmd, payload = packet[5], packet[6], packet[9:-1]
        if typ not in (0x1A, 0x1B):
            return
        if self.file_id is None:
            return
        if cmd == 7:
            if len(packet) < 97:
                raise RuntimeError(f'Incomplete audio header: {len(packet)} bytes')
            if struct.unpack_from('<I', packet, 9)[0] != self.file_id:
                raise RuntimeError('Audio header does not match requested recording')
            plain = aes(self.session_key, packet[79:95], packet[33:79])
            if len(plain) != 46 or plain[:14] != b'soundcored3200':
                raise RuntimeError('File key verification failed; refusing undeciphered audio')
            self.file_key, self.nonce = plain[14:], packet[17:33]
            self.report['file_key_verified'] = True
            log('Audio decryption ready')
        elif cmd in (8, 0x12):
            if self.file_key is None:
                raise RuntimeError('Audio arrived before verified file key')
            for offset in range(0, len(payload)-164, 166):
                sequence = struct.unpack_from('<I', payload, offset)[0]
                if self.last_sequence is not None:
                    if sequence <= self.last_sequence:
                        self.duplicates += 1
                        continue
                    self.gaps += max(0, sequence-self.last_sequence-1)
                self.last_sequence = sequence
                raw = aes(self.file_key, self.nonce[:12]+struct.pack('>I', (sequence*10)&0xFFFFFFFF), payload[offset+5:offset+165])
                try:
                    pcm = self.decoder.decode(raw)
                except ValueError:
                    self.frames_failed += 1
                    continue
                if self.first_audio is None:
                    self.first_audio = time.monotonic()
                    log('LIVE PCM AUDIO RECEIVED')
                self.frames_ok += 1
                self.last_audio = time.monotonic()
                self.phase = 'streaming'
                values = np.frombuffer(pcm, dtype='<i2').astype(np.float64)
                self.samples += len(values)
                self.peak = max(self.peak, float(np.max(np.abs(values), initial=0)))
                self.last_peak = float(np.max(np.abs(values), initial=0))/32768
                self.sum_squares += float(np.dot(values, values))
                if self.wave:
                    self.wave.writeframesraw(pcm)
                if self.pcm_callback:
                    self.pcm_callback(pcm)

    async def start_current(self, new_file=None):
        duration = 0
        if new_file is None:
            packet = await self.request(0x1B, 0x0E, b'\x00\x00')
            payload = packet[9:-1]
            count = int.from_bytes(payload[:2], 'little')
            pos = 2 + count*12
            if len(payload)<pos+8:
                raise RuntimeError('File list did not contain active recording metadata')
            new_file, duration = struct.unpack_from('<II', payload, pos)
        if not new_file:
            return False
        self.file_id = new_file
        self.phase = 'starting'
        self.file_key = None
        self.last_sequence = None
        self.decoder.close()
        self.decoder = Decoder()
        offset = (max(0, duration-1)*50)*160
        self.report['start_duration_seconds'] = duration
        self.report['requested_offset'] = offset
        log(f'Active recording found, duration={duration}; requesting realtime stream')
        await self.send(0x1A, 7, struct.pack('<IIB', offset, self.file_id, 1))
        return True

    async def run(self, seconds=30, start=False, wait_for_recording=False):
        identity = known_identity()
        def matches(device, adv):
            from recorder_files import candidate_from
            candidate = candidate_from(device, adv)
            return candidate is not None and candidate.identity_hex == identity.hex()
        log('Scanning for the selected recorder...')
        device = await BleakScanner.find_device_by_filter(matches, timeout=20)
        if not device:
            raise RuntimeError('Recorder not advertising; release phone connection and wake recorder')
        log('Recorder found; connecting...')
        self.phase = 'connecting'
        self.client = BleakClient(device, timeout=20, pair=False)
        async with self.client:
            await self.client.start_notify(NOTIFY, self.notify)
            info = await self.request(1, 1)
            body = info[9:-1]
            recording = len(body)>50 and body[50] == 1
            self.report.update(gatt_connected=True, firmware=body[3:8].decode('ascii',errors='replace'), recording_on_connect=recording)
            log(f'Device recording={recording}')
            await self.handshake()
            self.phase = 'waiting'
            if start and not recording:
                await self.send(0x18, 0x82, b'\x01')
                recording = True
            if not recording:
                if not wait_for_recording:
                    raise RuntimeError('No active recording. Press the physical recording button then retry.')
                log('Connected; waiting for the physical recording button')
            if self.save_audio:
                self.wave = wave.open(str(self.folder/'live-test.wav'), 'wb')
                self.wave.setparams((1,2,16000,0,'NONE','not compressed'))
            if recording:
                await self.start_current()
            deadline = time.monotonic()+seconds if seconds else float('inf')
            next_log = time.monotonic()+5
            next_health = time.monotonic()+5
            try:
                while time.monotonic()<deadline and not self.stopped:
                    try:
                        packet = await asyncio.wait_for(self.queue.get(), min(2, max(.01,deadline-time.monotonic())))
                    except asyncio.TimeoutError:
                        packet = None
                    if packet:
                        if packet[5:7] == b'\x1a\x06' and len(packet) >= 14:
                            new_file = struct.unpack_from('<I',packet,9)[0]
                            if new_file and new_file != self.file_id:
                                await self.start_current(new_file)
                        elif packet[5:7] == b'\x18\x82' and len(packet)>10 and packet[9] in (0,2):
                            self.file_id = None
                            self.file_key = None
                            self.last_peak = 0
                            self.phase = 'waiting'
                            log('Device recording stopped; waiting for the recording button')
                        elif packet[5:7] == b'\x18\x82' and len(packet)>10 and packet[9] == 1 and self.file_id is None:
                            await self.start_current()
                        else:
                            self.handle_audio(packet)
                    if not self.client.is_connected:
                        raise RuntimeError('Bluetooth disconnected')
                    now = time.monotonic()
                    # Catch missed button notifications and a stalled connection after sleep.
                    if now >= next_health and now-self.last_audio > 5:
                        info = await self.request(1,1)
                        body = info[9:-1]
                        if len(body)>50 and body[50] == 1:
                            await self.start_current()
                        else:
                            self.file_id = None
                            self.file_key = None
                            self.last_peak = 0
                            self.phase = 'waiting'
                        next_health = time.monotonic()+5
                    if now>=next_log:
                        log(f'audio frames={self.frames_ok}, errors={self.frames_failed}, seconds={self.samples/16000:.2f}, peak={self.peak/32768:.3f}')
                        next_log=now+30
            finally:
                # Stop transfer when cancelled/reconnecting; leave physical recording alone.
                if self.client.is_connected:
                    try:
                        async with asyncio.timeout(3):
                            await self.send(0x1A,0x11,b'\x00')
                            if start and not self.report['recording_on_connect']:
                                await self.send(0x18,0x82,b'\x02')
                    except Exception:
                        pass
                self.phase = 'disconnected'

    def close(self):
        if self.wave:
            self.wave.close()
        self.decoder.close()
        self.report.update(frames_decoded=self.frames_ok, decode_errors=self.frames_failed,
                           pcm_seconds=self.samples/16000, peak=self.peak/32768,
                           rms=math.sqrt(self.sum_squares/max(1,self.samples))/32768,
                           duplicate_frames=self.duplicates, missing_frames=self.gaps)
        (self.folder/'result.json').write_text(json.dumps(self.report,ensure_ascii=False,indent=2),encoding='utf-8')
        log(json.dumps(self.report,ensure_ascii=False))


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--seconds',type=int,default=30)
    parser.add_argument('--start',action='store_true')
    args=parser.parse_args()
    folder=ROOT/'diagnostics'/dt.datetime.now().strftime('%Y%m%d-%H%M%S-live-audio')
    folder.mkdir(parents=True,exist_ok=True)
    receiver=Receiver(folder)
    try:
        await receiver.run(args.seconds,args.start)
    except Exception as error:
        receiver.report['error']=f'{type(error).__name__}: {error}'
        log(receiver.report['error'])
    finally:
        receiver.close()


if __name__=='__main__':
    asyncio.run(main())

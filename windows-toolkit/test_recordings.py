import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import recorder_files as files
import soundcore_stream as protocol


def packet(command, payload=b'', typ=0x1A):
    raw = b'\x09\xff\0\0\x01'+bytes([typ, command])+struct.pack('<H', len(payload)+10)+payload
    return raw+bytes([sum(raw)&255])


class Decoder:
    def decode(self, data):
        assert data[:4] == b'test'
        return b'\x01\0'*320
    def close(self):
        pass


class RecordingTests(unittest.TestCase):
    def test_advertised_identity_survives_address_change_and_ignores_headphones(self):
        adv = SimpleNamespace(service_uuids=[protocol.SERVICE], service_data={}, local_name='soundcore Work',
                              manufacturer_data={0x0201:bytes.fromhex('030405068100')}, rssi=-45)
        first = files.candidate_from(SimpleNamespace(name=None, address='random-one'), adv)
        second = files.candidate_from(SimpleNamespace(name=None, address='random-two'), adv)
        self.assertEqual(first.identity_hex, '010203040506')
        self.assertEqual(first.identity_hex, second.identity_hex)
        self.assertTrue(first.bound_advertised)
        adv.service_uuids=[]; adv.local_name='soundcore Headphones'
        self.assertIsNone(files.candidate_from(SimpleNamespace(name=None), adv))
        adv.local_name=None
        self.assertIsNone(files.candidate_from(SimpleNamespace(name=None), adv),
                          'unnamed nearby BLE devices must not inherit the display fallback as a model hint')

    def test_fresh_install_has_no_implicit_device_and_remembers_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.assertIsNone(files.load_selection(root))
            item=files.Candidate('010203040506','Work',-40,True)
            files.save_selection(root,item)
            self.assertEqual(files.load_selection(root)['identity_hex'],item.identity_hex)
            target=root/'中文目录 & recordings'
            files.set_download_folder(root,target)
            self.assertEqual(files.download_folder(root).resolve(),target.resolve())

    def test_list_requires_complete_packet_and_marks_active(self):
        payload=struct.pack('<HIIIII',1,123,456,160,123,5)
        count, entries, current, _=files.parse_list(packet(14,payload,0x1B))
        self.assertEqual(count,1)
        self.assertTrue(entries[0].active)
        with self.assertRaises(ValueError): files.parse_list(packet(14,payload[:-1],0x1B))

    def writer(self, root, size=320):
        result=files.WavExport(root,'010203040506',files.Recording(123,125,size),bytes(32),Decoder)
        nonce=b'N'*16; file_key=b'K'*32; session_nonce=b'S'*16
        header=(struct.pack('<II',123,size)+nonce+
                protocol.aes(bytes(32),session_nonce,b'soundcored3200'+file_key)+session_nonce+b'\0')
        result.feed(packet(7,header))
        return result, nonce, file_key

    def slice(self, sequence, nonce, key):
        encrypted=protocol.aes(key,nonce[:12]+struct.pack('>I',sequence*10),b'test'+bytes(156))
        return packet(8,struct.pack('<IB',sequence,0)+encrypted+b'\0')

    def test_success_requires_done_exact_size_and_preserves_existing_download(self):
        with tempfile.TemporaryDirectory() as root:
            existing=Path(root)/'010203040506_123.wav'; existing.write_bytes(b'keep me')
            writer,nonce,key=self.writer(root)
            try:
                writer.feed(self.slice(0,nonce,key)); writer.feed(self.slice(1,nonce,key))
                with self.assertRaises(ValueError): writer.finish()
                writer.feed(packet(10))
                result=writer.finish()
                files.verify_download(result)
                self.assertEqual(result['seconds'],.04)
                self.assertEqual(existing.read_bytes(),b'keep me')
                self.assertNotEqual(result['path'],str(existing))
            finally: writer.close()

    def test_missing_frame_incomplete_size_and_cancel_leave_no_final_wav(self):
        for mode in ('gap','incomplete','cancel'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                writer,nonce,key=self.writer(root)
                try:
                    writer.feed(self.slice(0,nonce,key))
                    if mode=='gap':
                        with self.assertRaises(ValueError): writer.feed(self.slice(2,nonce,key))
                    elif mode=='incomplete':
                        writer.feed(packet(10))
                        with self.assertRaises(ValueError): writer.finish()
                finally: writer.close()
                self.assertFalse(list(Path(root).iterdir()))


class DownloadDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_or_cancelled_download_never_deletes(self):
        for error in (RuntimeError('transfer failed'),asyncio.CancelledError()):
            session=SimpleNamespace(export=AsyncMock(side_effect=error),delete=AsyncMock())
            with self.assertRaises(type(error)):
                await files.download_one(session,files.Recording(123,0,160),'.',True)
            session.delete.assert_not_called()

    async def test_delete_only_after_file_verified_and_receipt_saved(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'record.wav'; path.write_bytes(b'validated audio')
            result={'path':str(path),'complete':True,'bytes':path.stat().st_size,
                    'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            async def delete(file_id):
                self.assertEqual(file_id,123)
                self.assertTrue(path.with_suffix('.wav.json').exists())
            session=SimpleNamespace(export=AsyncMock(return_value=result),delete=AsyncMock(side_effect=delete))
            await files.download_one(session,files.Recording(123,0,160),root,True)
            session.delete.assert_awaited_once_with(123)
            self.assertTrue(json.loads(path.with_suffix('.wav.json').read_text())['deleted_from_device'])

    async def test_modified_download_never_deletes(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'record.wav'; path.write_bytes(b'changed')
            result={'path':str(path),'complete':True,'bytes':7,'sha256':'wrong'}
            session=SimpleNamespace(export=AsyncMock(return_value=result),delete=AsyncMock())
            with self.assertRaises(ValueError):
                await files.download_one(session,files.Recording(123,0,160),root,True)
            session.delete.assert_not_called()

    async def test_repeated_pages_rejected(self):
        entries=b''.join(struct.pack('<III',i,10,160) for i in range(1,11))
        response=packet(14,struct.pack('<H',10)+entries+bytes(8),0x1B)
        session=files.RecorderSession('010203040506')
        session.request=AsyncMock(return_value=response)
        try:
            with self.assertRaises(ValueError): await session.list_files()
        finally: session.decoder.close()

    async def test_active_recording_cannot_be_deleted(self):
        session=files.RecorderSession('010203040506')
        session.list_files=AsyncMock(return_value=[files.Recording(123,0,160,True)])
        session.request=AsyncMock()
        try:
            with self.assertRaises(ValueError): await session.delete(123)
            session.request.assert_not_called()
        finally: session.decoder.close()

    async def test_historical_download_waits_until_device_stops_recording(self):
        session=files.RecorderSession('010203040506')
        completed=files.Recording(123,125,160)
        session.list_files=AsyncMock(return_value=[completed,files.Recording(124,0,0,True)])
        session.send=AsyncMock()
        try:
            with self.assertRaisesRegex(ValueError,'仍在录音'):
                await session.export(completed,'.')
            session.send.assert_not_called()
        finally: session.decoder.close()


if __name__=='__main__': unittest.main()

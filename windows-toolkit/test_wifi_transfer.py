import asyncio
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import xml.etree.ElementTree as ET
import time

from recorder_files import Recording, RecordingStarted
from test_recordings import packet
from wifi_transfer import PacketBuffer, WifiTransfer, endpoint_from, file_request
from windows_wifi import profile_xml


class WifiProtocolTests(unittest.TestCase):
    def test_fragmented_and_coalesced_frames_have_no_loss(self):
        expected = [packet(7, bytes(87)), packet(8, bytes(166 * 8)), packet(10)]
        wire = b''.join(expected)
        for size in (1, 7, 9, 160, 4096):
            parser, actual = PacketBuffer(), []
            for offset in range(0, len(wire), size):
                actual.extend(parser.feed(wire[offset:offset+size]))
            self.assertEqual(actual, expected)
            self.assertFalse(parser.data)

    def test_corrupt_frame_is_rejected(self):
        data = bytearray(packet(8, bytes(166)))
        data[20] ^= 1
        with self.assertRaisesRegex(ValueError, 'checksum'):
            PacketBuffer().feed(data)

    def test_endpoint_wire_order_and_sdk_request(self):
        self.assertEqual(endpoint_from(packet(5, b'\1\4\xa8\xc0\xbb\1')), ('192.168.4.1', 443))
        self.assertEqual(file_request(0x12345678), '08ee0000001a07120000000000785634121b')
        with self.assertRaises(ValueError):
            endpoint_from(packet(5, b'\x08\x08\x08\x08\xbb\1'))

    def test_temporary_profile_is_manual_and_xml_escaped(self):
        xml = ET.fromstring(profile_xml('Test & profile', '录音豆 & test', 'ABcd<>&12345'))
        ns = {'w': 'http://www.microsoft.com/networking/WLAN/profile/v1'}
        self.assertEqual(xml.find('w:connectionMode', ns).text, 'manual')
        self.assertEqual(xml.find('w:MSM/w:security/w:sharedKey/w:keyMaterial', ns).text, 'ABcd<>&12345')


class WifiFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_early_cancel_sends_finish_without_requesting_any_audio(self):
        sent=[]
        session=SimpleNamespace(restore_after_wifi=AsyncMock())
        transfer=WifiTransfer(session,progress=lambda _:None)
        transfer.opened=transfer.ble_detached=True
        transfer.endpoint=('192.168.43.1',443)
        transfer.credentials=('temporary-test','12345678')
        transfer.configured_at=time.monotonic()-20
        adapter=SimpleNamespace(current=lambda:{'connected':False},join=AsyncMock(),
            restore=AsyncMock(return_value={'restored':True}),close=lambda:None)
        transfer.wifi=adapter
        async def connect():
            transfer.ws=SimpleNamespace(settimeout=lambda _:None,send=sent.append,close=lambda **_:None)
        transfer._connect_socket=connect
        await transfer.__aexit__(asyncio.CancelledError,None,None)
        self.assertEqual(sent[0],'FINISH')
        self.assertEqual(len(sent),2)
        self.assertNotIn('1a07',''.join(sent))
        adapter.join.assert_awaited_once()
        adapter.restore.assert_awaited_once()
        session.restore_after_wifi.assert_awaited_once()
        self.assertTrue(transfer.report['cleanup_wss_connected'])
        self.assertIsNone(transfer.credentials)

    async def test_recording_blocks_hotspot_before_config(self):
        session = SimpleNamespace(list_files=AsyncMock(return_value=[Recording(123, 0, 0, True)]),
                                  is_recording=lambda:True, check_idle=lambda:None, send=AsyncMock())
        adapter = SimpleNamespace(original={}, restore=AsyncMock(return_value={'restored': True}), close=lambda: None)
        with patch('wifi_transfer.TemporaryWifi', return_value=adapter):
            with self.assertRaisesRegex(RecordingStarted, '正在录音'):
                async with WifiTransfer(session):
                    pass
        session.send.assert_not_called()
        adapter.restore.assert_awaited_once()

    async def test_early_socket_close_never_commits_file(self):
        with tempfile.TemporaryDirectory() as folder:
            session = SimpleNamespace(identity=bytes(6), session_key=bytes(32), check_idle=lambda:None)
            transfer = WifiTransfer(session)
            transfer.ws = SimpleNamespace(send=lambda _: None, recv=lambda: b'')
            with self.assertRaises(ConnectionError):
                await transfer.export(Recording(123, 125, 160), Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])

    async def test_recording_notification_aborts_without_committing(self):
        with tempfile.TemporaryDirectory() as folder:
            session = SimpleNamespace(identity=bytes(6), session_key=bytes(32), check_idle=lambda:None)
            transfer = WifiTransfer(session)
            transfer.ws = SimpleNamespace(send=lambda _: None, recv=lambda: packet(6, struct.pack('<I', 456)))
            with self.assertRaises(RecordingStarted):
                await transfer.export(Recording(123, 125, 160), Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == '__main__':
    unittest.main()

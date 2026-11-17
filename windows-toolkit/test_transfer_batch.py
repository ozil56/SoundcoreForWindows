import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from auto_sync import AutoPipeline
from recorder_files import Candidate, Recording, RecordingStarted, RecorderSession, download_one, save_selection
from soundcore_stream import Receiver
import soundcore_stream as protocol
from test_auto_sync import make_receipt
from test_recordings import packet
from tool_settings import update_settings
from transfer_batch import DownloadBatch, TransferRecoveryError


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def make_wifi(self, root, events, fail_enter=None, fail_export=None):
        class Link:
            def __init__(self, session, report, notice):
                self.report=report
            async def __aenter__(self):
                events.append('hotspot open')
                if fail_enter: raise fail_enter
                return self
            async def __aexit__(self,*_):
                events.append('BLE restored')
                self.report['ble_restored']=True
            async def export(self,item,folder,progress):
                events.append(('wifi',item.file_id))
                if fail_export: raise fail_export
                progress(160,160)
                return dict(make_receipt(folder,item.file_id),transport='wifi',transfer_seconds=1)
        return Link

    async def test_batch_opens_one_hotspot_and_defers_deletion_until_ble_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);events=[]
            save_selection(root,Candidate('010203040506','test',-40,False))
            update_settings(root,auto_sync=True,auto_delete=True,schedule_enabled=False)
            pipeline=AutoPipeline(root,lambda _:None)
            receiver=SimpleNamespace(recording_active=False,file_id=None,phase='waiting')
            remaining=[Recording(1700000000+i,0,2000) for i in range(2)]
            session=SimpleNamespace(list_files=AsyncMock(side_effect=lambda:remaining.copy()),check_idle=lambda:None)
            async def delete(file_id):
                self.assertIn('BLE restored',events)
                events.append(('delete',file_id))
                remaining[:]=[f for f in remaining if f.file_id != file_id]
            session.delete=AsyncMock(side_effect=delete)
            pipeline.receiver_id=id(receiver);pipeline.idle_since=time.monotonic()-20
            with patch('auto_sync.RecorderSession.attached',return_value=session), \
                    patch('transfer_batch.WifiTransfer',self.make_wifi(root,events)):
                await pipeline.when_idle(receiver)
            self.assertEqual(events,['hotspot open',('wifi',1700000000),('wifi',1700000001),
                                     'BLE restored',('delete',1700000000),('delete',1700000001)])
            self.assertEqual(pipeline.library.counts()['pending'],2)
            self.assertEqual(pipeline.last_error,'')

    async def test_unavailable_wifi_falls_back_once_for_whole_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            events=[]
            session=SimpleNamespace(check_idle=lambda:None,
                export=AsyncMock(side_effect=lambda item,folder,p:make_receipt(folder,item.file_id)))
            with patch('transfer_batch.WifiTransfer',self.make_wifi(directory,events,fail_enter=RuntimeError('battery low'))):
                async with DownloadBatch(session,directory,{}) as batch:
                    for index in range(2):
                        result=await download_one(batch,Recording(123+index,0,2000),directory)
                        self.assertEqual(result['transport'],'ble')
            self.assertEqual(events,['hotspot open'])
            self.assertEqual(session.export.await_count,2)

    async def test_failed_wifi_file_restores_before_ble_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            events=[]
            async def ble(item,folder,progress):
                self.assertEqual(events[-1],'BLE restored')
                return make_receipt(folder,item.file_id)
            session=SimpleNamespace(check_idle=lambda:None,export=AsyncMock(side_effect=ble))
            with patch('transfer_batch.WifiTransfer',self.make_wifi(directory,events,fail_export=ConnectionError('lost'))):
                async with DownloadBatch(session,directory,{}) as batch:
                    result=await batch.export(Recording(123,0,2000),directory)
            self.assertEqual(result['transport'],'ble')
            session.export.assert_awaited_once()

    async def test_cancel_and_recording_never_trigger_ble_fallback(self):
        for error in (asyncio.CancelledError(),RecordingStarted('recording')):
            with self.subTest(error=type(error)),tempfile.TemporaryDirectory() as directory:
                events=[];session=SimpleNamespace(check_idle=lambda:None,export=AsyncMock())
                with patch('transfer_batch.WifiTransfer',self.make_wifi(directory,events,fail_export=error)):
                    with self.assertRaises(type(error)):
                        async with DownloadBatch(session,directory,{}) as batch:
                            await batch.export(Recording(123,0,2000),directory)
                self.assertEqual(events[-1],'BLE restored')
                session.export.assert_not_awaited()

    async def test_disabled_wifi_uses_ble_without_opening_hotspot(self):
        with tempfile.TemporaryDirectory() as directory:
            session=SimpleNamespace(check_idle=lambda:None,export=AsyncMock(return_value={}))
            with patch('transfer_batch.WifiTransfer') as wifi:
                async with DownloadBatch(session,directory,{'wifi_fast_transfer':False}) as batch:
                    await batch.export(Recording(123,0,2000),directory)
                wifi.assert_not_called()

    async def test_recovery_error_is_not_hidden_by_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            events=[];session=SimpleNamespace(check_idle=lambda:None,export=AsyncMock())
            link=self.make_wifi(directory,events,fail_export=ConnectionError('lost'))
            async def fail_close(self,*_): self.report['recovery_errors']=['Bluetooth unavailable']
            link.__aexit__=fail_close
            with patch('transfer_batch.WifiTransfer',link):
                with self.assertRaises(TransferRecoveryError):
                    async with DownloadBatch(session,directory,{}) as batch:
                        await batch.export(Recording(123,0,2000),directory)
            session.export.assert_not_awaited()


class RestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_device_can_reconnect_while_windows_link_suppresses_advertising(self):
        identity='010203040506'
        observed_device=SimpleNamespace(address='observed-device')
        protocol.DEVICE_CACHE[identity]=observed_device
        session=RecorderSession(identity)
        body=bytearray(65);body[3:8]=b'04.73'
        session.request=AsyncMock(return_value=packet(1,body,1))
        session.handshake=AsyncMock()
        client=SimpleNamespace(is_connected=True,connect=AsyncMock(),start_notify=AsyncMock(),
            disconnect=AsyncMock(),write_gatt_char=AsyncMock(),services=SimpleNamespace(get_characteristic=lambda _:True))
        try:
            with patch('recorder_files.BleakClient',return_value=client) as factory, \
                    patch('recorder_files.BleakScanner.find_device_by_filter') as scan:
                async with session:
                    self.assertIs(session.ble_device,observed_device)
                scan.assert_not_called()
                self.assertIs(factory.call_args.args[0],observed_device)
                client.disconnect.assert_awaited_once()
        finally:
            protocol.DEVICE_CACHE.pop(identity,None)

    async def test_attached_restore_updates_original_receiver_and_notification_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver=Receiver(Path(directory),save_audio=False)
            old_client=SimpleNamespace(is_connected=True,disconnect=AsyncMock())
            receiver.client=old_client
            receiver.ble_device=SimpleNamespace(address='test-device')
            restored_client=SimpleNamespace(connect=AsyncMock(),start_notify=AsyncMock(),disconnect=AsyncMock(),
                                            services=SimpleNamespace(get_characteristic=lambda _:True))
            receiver.session_key=bytes(32)
            session=RecorderSession.attached(receiver,'010203040506')
            session.send=AsyncMock()
            session.receive=AsyncMock(return_value=packet(2))
            info=bytearray(65);info[3:8]=b'04.73'
            async def request(typ,cmd,payload=b''):
                if (typ,cmd)==(1,1): return packet(1,info,1)
                private=protocol.ec.generate_private_key(protocol.ec.SECP256R1())
                peer=protocol.ec.EllipticCurvePublicKey.from_encoded_point(protocol.ec.SECP256R1(),payload)
                public=private.public_key().public_bytes(protocol.serialization.Encoding.X962,
                    protocol.serialization.PublicFormat.UncompressedPoint)
                shared=private.exchange(protocol.ec.ECDH(),peer)
                return packet(1,public+shared,0x2E)
            session.request=request
            receiver.buffer.extend(b'old partial data');receiver.queue.put_nowait(b'old event')
            try:
                with patch('recorder_files.BleakClient',return_value=restored_client):
                    await session.restore_after_wifi()
                self.assertIs(receiver.client,restored_client)
                old_client.disconnect.assert_awaited_once()
                self.assertEqual(receiver.session_key,session.session_key)
                self.assertNotEqual(receiver.session_key,bytes(32))
                self.assertTrue(receiver.report['handshake_verified'])
                restored_client.start_notify.assert_awaited_once()
                self.assertEqual(restored_client.start_notify.await_args.args[1],receiver.notify)
                self.assertEqual(session.info['firmware'],'04.73')
                self.assertFalse(receiver.buffer)
                self.assertTrue(receiver.queue.empty())
            finally:
                receiver.decoder.close()


if __name__=='__main__': unittest.main()

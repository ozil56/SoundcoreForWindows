import asyncio
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from auto_sync import AutoPipeline, Library, dated_target
from recorder_files import (Recording, RecorderSession, RecordingStarted, Candidate,
                            save_selection, set_download_folder)
from tool_settings import in_schedule, read_settings, update_settings, write_json
from test_recordings import packet
import soundcore_stream as protocol


def make_receipt(root, file_id=1700000000, content=b'complete test audio'):
    audio = Path(root)/f'010203040506_{file_id}.wav'
    audio.parent.mkdir(parents=True,exist_ok=True)
    audio.write_bytes(content)
    receipt = {'path':str(audio),'complete':True,'bytes':len(content),
               'sha256':hashlib.sha256(content).hexdigest(),'identity_hex':'010203040506',
               'file_id':file_id,'frames':100,'seconds':2,'list_size':2000}
    write_json(audio.with_suffix('.wav.json'),receipt)
    return receipt


class ScheduleTests(unittest.TestCase):
    def test_settings_panel_saves_cross_midnight_without_pausing_bridge(self):
        # Use the entry point's Tcl/Tk bootstrap, including standalone test runs.
        from soundcore_microphone import tk
        from sync_panel import SyncPanel
        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk()
            root.withdraw()
            pipeline = AutoPipeline(folder,lambda _:None)
            app = SimpleNamespace(root=root,pipeline=pipeline,message=lambda _:None,sync_panel=None)
            panel = SyncPanel(app,folder)
            app.sync_panel = panel
            try:
                panel.start.set('20:00')
                panel.end.set('08:00')
                panel.auto_sync.set(True)
                panel.auto_delete.set(True)
                panel.save()
                root.update()
                self.assertEqual(read_settings(folder)['schedule_start'],'20:00')
                self.assertEqual(read_settings(folder)['schedule_end'],'08:00')
                self.assertTrue(read_settings(folder)['auto_delete'])
                self.assertGreater(panel.window.winfo_height(),700)
                self.assertIn('队列',panel.status.get())
            finally:
                panel.close()
                root.destroy()

    def test_cross_midnight_and_end_exclusive(self):
        settings = {'schedule_start':'20:00','schedule_end':'08:00'}
        for stamp, expected in [('2026-11-12T19:59',False),('2026-11-12T20:00',True),
                ('2026-11-12T23:59',True),('2026-11-13T00:00',True),
                ('2026-11-13T07:59',True),('2026-11-13T08:00',False),
                ('2026-11-13T12:00',False),('2026-11-13T20:00',True)]:
            with self.subTest(stamp=stamp):
                self.assertEqual(in_schedule(settings,dt.datetime.fromisoformat(stamp)),expected)
        self.assertTrue(in_schedule(dict(settings,schedule_enabled=False),dt.datetime(2026,1,1,12)))
        self.assertTrue(in_schedule({'schedule_start':'08:00','schedule_end':'08:00'}))

    def test_daytime_window(self):
        settings = {'schedule_start':'09:00','schedule_end':'17:30'}
        self.assertTrue(in_schedule(settings,dt.datetime(2026,1,1,10)))
        self.assertFalse(in_schedule(settings,dt.datetime(2026,1,1,17,30)))
        self.assertFalse(in_schedule(settings,dt.datetime(2026,1,1,0)))

    def test_folder_change_preserves_automation_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            update_settings(folder,auto_sync=True,auto_delete=True,schedule_start='20:00',schedule_end='08:00')
            set_download_folder(folder,Path(folder)/'audio')
            self.assertTrue(read_settings(folder)['auto_delete'])
            self.assertEqual(read_settings(folder)['schedule_start'],'20:00')


class QueueTests(unittest.TestCase):
    def test_immediate_jobs_are_device_scoped_durable_and_do_not_rerun_finished_text(self):
        with tempfile.TemporaryDirectory() as folder:
            pipeline=AutoPipeline(folder,lambda _:None)
            library=pipeline.library
            keys=[]
            for i in range(5):
                receipt=make_receipt(folder,1700000000+i)
                if i==4: receipt['identity_hex']='010203040507'
                keys.append(library.register(receipt))
            library.update(keys[1],status='complete',output='original.txt',output_root=folder)
            library.update(keys[2],status='cancelled')
            library.update(keys[3],status='failed',retry_at=time.time()+300)
            snapshot=pipeline.vocabulary.snapshot()
            manual=library.queue_manual(keys[0],snapshot)
            library.request_device_transcription('010203040506')
            restored=Library(folder)
            for i in (0,3):
                self.assertTrue(restored.data['jobs'][keys[i]]['run_immediately'])
                self.assertEqual(restored.data['jobs'][keys[i]]['retry_at'],0)
            for key in (keys[1],keys[2],keys[4],manual):
                self.assertNotIn('run_immediately',restored.data['jobs'][key])
            self.assertEqual(restored.next_job(folder,allow_auto=False)[0],manual)
            restored.update(manual,status='complete')
            self.assertEqual(restored.next_job(folder,allow_auto=False)[0],keys[0])
            restored.update(keys[0],status='complete')
            restored.update(keys[3],status='complete')
            self.assertIsNone(restored.next_job(folder,allow_auto=False))
            self.assertEqual(restored.data['jobs'][keys[1]]['output'],'original.txt')

    def test_immediate_transcription_finishes_with_automation_off(self):
        with tempfile.TemporaryDirectory() as folder:
            pipeline=AutoPipeline(folder,lambda _:None)
            key=pipeline.library.register(make_receipt(folder))
            pipeline.library.request_device_transcription('010203040506')
            task=pipeline.library.next_job(Path(folder)/'texts',allow_auto=False)
            pipeline.process=SimpleNamespace(stdin=io.StringIO())
            pipeline.output_lines.put({'event':'result','segments':[{'start':0,'end':2,'text':'立即转写测试'}]})
            with patch.object(pipeline,'_start_process'), patch('auto_sync.in_schedule',return_value=False):
                pipeline.transcribe(*task,Path(folder)/'texts')
            job=pipeline.library.data['jobs'][key]
            self.assertEqual(job['status'],'complete')
            self.assertIn('立即转写测试',Path(job['output']).read_text(encoding='utf-8-sig'))
            self.assertIn('vocabulary',job)
            self.assertIsNone(pipeline.library.next_job(Path(folder)/'texts',allow_auto=False))

    def test_missing_selection_or_model_does_not_create_immediate_request(self):
        with tempfile.TemporaryDirectory() as folder:
            pipeline=AutoPipeline(folder,lambda _:None)
            with self.assertRaisesRegex(ValueError,'选择录音豆'):
                pipeline.request_device_sync()
            save_selection(folder,Candidate('010203040506','test',-40,False))
            with self.assertRaisesRegex(RuntimeError,'本地模型'):
                pipeline.request_device_sync()
            self.assertIsNone(pipeline.immediate_identity)

    def test_verified_receipts_skip_repeated_downloads_but_detect_damage(self):
        with tempfile.TemporaryDirectory() as folder:
            receipt = make_receipt(folder)
            library = Library(folder)
            self.assertEqual(library.import_folder(folder),[])
            self.assertEqual(library.import_folder(folder),[])
            self.assertEqual(library.counts()['pending'],1)
            self.assertTrue(library.has_download('010203040506',Recording(receipt['file_id'],0,2000)))
            self.assertFalse(library.has_download('010203040506',Recording(receipt['file_id'],0,3000)))
            Path(receipt['path']).write_bytes(b'changed')
            self.assertFalse(library.has_download('010203040506',Recording(receipt['file_id'],0,2000)))

    def test_restart_requeues_running_job_and_preserves_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            receipt = make_receipt(folder)
            library = Library(folder)
            key = library.register(receipt)
            library.update(key,status='running')
            restored = Library(folder)
            self.assertEqual(restored.next_job(folder)[1]['status'],'pending')
            restored.update(key,status='complete',output_root=str(Path(folder).resolve()))
            self.assertIsNone(Library(folder).next_job(folder))

    def test_text_uses_recording_date_and_preserves_existing_text(self):
        with tempfile.TemporaryDirectory() as folder:
            # Around year boundary, use local timestamps to verify local date.
            stamp = int(dt.datetime(2025,12,31,23,59).timestamp())
            receipt = make_receipt(folder,stamp)
            output_root = Path(folder)/'texts'
            pipeline = AutoPipeline(folder,lambda _:None)
            result = {'model':'local-test','segments':[{'start':0,'end':2,'text':'中文测试'}]}
            output = pipeline.publish_text(receipt,result,output_root)
            self.assertEqual(output.parent,output_root/'2025'/'12'/'31')
            self.assertIn('中文测试',output.read_text(encoding='utf-8-sig'))
            self.assertEqual(pipeline.publish_text(receipt,result,output_root),output)
            output.write_text('用户修改后的文本',encoding='utf-8')
            self.assertNotEqual(pipeline.publish_text(receipt,result,output_root),output)
            self.assertEqual(output.read_text(encoding='utf-8'),'用户修改后的文本')


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_batch_bypasses_schedule_and_switches_then_returns_to_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            update_settings(root,auto_sync=False,auto_transcribe=False,auto_delete=True)
            save_selection(root,Candidate('010203040506','test',-40,False))
            original_settings = read_settings(root)
            pipeline = AutoPipeline(root,lambda _:None)
            receiver = SimpleNamespace(recording_active=False,file_id=None,phase='waiting')
            files = [Recording(1700000000+i,0,2000) for i in range(2)]
            session = SimpleNamespace(list_files=AsyncMock(side_effect=lambda:files.copy()),check_idle=lambda:None)
            async def export(item, output, progress):
                session.transfer_guard()  # Guard must allow this explicit batch too.
                return make_receipt(output,item.file_id)
            session.export = AsyncMock(side_effect=export)
            async def delete(file_id):
                files[:] = [f for f in files if f.file_id != file_id]
            session.delete = AsyncMock(side_effect=delete)
            with patch.object(pipeline,'runtime'), patch('auto_sync.in_schedule',return_value=False), \
                    patch('auto_sync.RecorderSession.attached',return_value=session):
                self.assertTrue(pipeline.request_device_sync())
                self.assertFalse(pipeline.request_device_sync())
                pipeline.receiver_id=id(receiver)
                pipeline.idle_since=time.monotonic()-20
                for _ in range(3):
                    await pipeline.when_idle(receiver)
                self.assertFalse(pipeline.snapshot()['immediate_download_pending'])
                self.assertEqual(session.export.await_count,2)
                self.assertEqual(session.delete.await_count,2)
                self.assertTrue(all(j['run_immediately'] for j in pipeline.library.data['jobs'].values()))
                self.assertIsNotNone(pipeline.library.next_job(root/'text',allow_auto=False))
                # Another file after this batch must wait for the original schedule.
                files.append(Recording(1700000009,0,2000))
                pipeline.next_sync=0
                await pipeline.when_idle(receiver)
                self.assertEqual(session.export.await_count,2)
            self.assertEqual(read_settings(root),original_settings)

    async def test_immediate_request_survives_recording_and_connection_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            save_selection(folder,Candidate('010203040506','test',-40,False))
            pipeline=AutoPipeline(folder,lambda _:None)
            receiver=SimpleNamespace(recording_active=True,file_id=123,phase='streaming')
            session=SimpleNamespace(list_files=AsyncMock(side_effect=ConnectionError('disconnected')))
            with patch.object(pipeline,'runtime'), patch('auto_sync.RecorderSession.attached',return_value=session):
                pipeline.request_device_sync()
                await pipeline.when_idle(receiver)
                session.list_files.assert_not_awaited()
                self.assertTrue(pipeline.immediate_identity)
                receiver.recording_active=False; receiver.file_id=None
                pipeline.idle_since=time.monotonic()-20
                await pipeline.when_idle(receiver)
                self.assertTrue(pipeline.immediate_identity)
                self.assertIn('ConnectionError',pipeline.last_error)
                session.list_files.side_effect=None
                session.list_files.return_value=[]
                pipeline.next_sync=0
                await pipeline.when_idle(receiver)
                self.assertIsNone(pipeline.immediate_identity)

    async def test_switching_device_does_not_transfer_immediate_request(self):
        with tempfile.TemporaryDirectory() as folder:
            save_selection(folder,Candidate('010203040506','first',-40,False))
            pipeline=AutoPipeline(folder,lambda _:None)
            with patch.object(pipeline,'runtime'):
                pipeline.request_device_sync()
            save_selection(folder,Candidate('010203040507','second',-40,False))
            receiver=SimpleNamespace(recording_active=False,file_id=None,phase='waiting')
            with patch('auto_sync.RecorderSession.attached') as attach:
                await pipeline.when_idle(receiver)
                attach.assert_not_called()
            self.assertIsNone(pipeline.immediate_identity)

    async def test_delete_rechecks_local_file_and_active_state(self):
        with tempfile.TemporaryDirectory() as folder:
            update_settings(folder,auto_delete=True)
            pipeline = AutoPipeline(folder,lambda _:None)
            receipt = make_receipt(folder)
            session = SimpleNamespace(check_idle=lambda:None,delete=AsyncMock())
            Path(receipt['path']).write_bytes(b'corrupted')
            with self.assertRaises(ValueError):
                await pipeline.delete_saved(session,receipt)
            session.delete.assert_not_awaited()
            receipt = make_receipt(folder)
            await pipeline.delete_saved(session,receipt)
            session.delete.assert_awaited_once_with(receipt['file_id'])
            saved = json.loads(Path(receipt['path']).with_suffix('.wav.json').read_text(encoding='utf-8'))
            self.assertTrue(saved['deleted_from_device'])

    async def test_failed_delete_reuses_saved_download_on_next_cycle(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            update_settings(root,auto_sync=True,auto_delete=True,schedule_enabled=False)
            save_selection(root,Candidate('010203040506','test',-40,False))
            pipeline = AutoPipeline(root,lambda _:None)
            receiver = SimpleNamespace(recording_active=False,file_id=None,phase='waiting',recording_generation=0)
            recording = Recording(1700000000,0,2000)
            files = [recording]
            session = SimpleNamespace(list_files=AsyncMock(side_effect=lambda:files.copy()),check_idle=lambda:None)
            async def export(item,output,progress):
                return make_receipt(output,item.file_id)
            session.export = AsyncMock(side_effect=export)
            attempts = []
            async def delete(file_id):
                attempts.append(file_id)
                if len(attempts)==1:
                    raise ConnectionError('disconnect during delete')
                files.clear()
            session.delete = AsyncMock(side_effect=delete)
            with patch('auto_sync.RecorderSession.attached',return_value=session):
                for _ in range(2):
                    pipeline.receiver_id=id(receiver)
                    pipeline.idle_since=time.monotonic()-20
                    pipeline.next_sync=0
                    await pipeline.when_idle(receiver)
            self.assertEqual(session.export.await_count,1)
            self.assertEqual(session.delete.await_count,2)
            self.assertEqual(pipeline.library.counts()['pending'],1)

    async def test_start_notification_cancels_transfer_and_no_complete_file(self):
        with tempfile.TemporaryDirectory() as folder:
            receiver = protocol.Receiver(Path(folder),save_audio=False)
            receiver.client=SimpleNamespace(is_connected=True)
            receiver.session_key=bytes(32)
            session=RecorderSession.attached(receiver,'010203040506')
            session.list_files=AsyncMock(return_value=[Recording(123,0,320)])
            async def send(typ,cmd,payload):
                if cmd==7:
                    receiver.notify(None,packet(6,struct.pack('<I',456)))
            session.send=AsyncMock(side_effect=send)
            try:
                with self.assertRaises(RecordingStarted):
                    await session.export(Recording(123,0,320),folder)
                self.assertTrue(receiver.recording_active)
                self.assertFalse(list(Path(folder).glob('*.wav')))
                session.send.assert_any_await(0x1A,0x11,b'\0')
            finally:
                receiver.decoder.close()


if __name__=='__main__':
    unittest.main()

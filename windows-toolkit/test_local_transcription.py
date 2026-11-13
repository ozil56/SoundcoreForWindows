import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auto_sync import AutoPipeline, Library
from local_transcription_panel import submit_local_batch
from test_auto_sync import make_receipt
from tool_settings import imported_audio_folder, update_settings


class LocalTranscriptionTests(unittest.TestCase):
    def test_import_and_scan_never_create_automatic_job_even_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = Path(make_receipt(root/'source')['path'])
            library = Library(root/'data')
            key = library.import_audio(source, root/'imports')
            self.assertEqual(library.data['jobs'], {})
            self.assertEqual(library.records()[0]['title'], source.stem)
            self.assertEqual(library.import_folder(root/'imports'), [])
            restored = Library(root/'data')
            restored.import_folder(root/'imports')
            self.assertIsNone(restored.next_job(root/'texts', True))
            self.assertEqual(restored.import_audio(source, root/'imports'), key)
            self.assertEqual(len(restored.records()), 1)
            self.assertEqual(source.read_bytes(), b'complete test audio')

    def test_legacy_external_jobs_preserve_results_without_automatic_requeue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            library = Library(root/'data')
            key = library.import_audio(Path(make_receipt(root/'source')['path']), root/'imports')
            receipt = library.records()[0]['receipt']
            result = root/'old.txt'; result.write_text('原结果', encoding='utf-8')
            old = {'receipt':receipt, 'status':'complete', 'output':str(result), 'output_root':str(root)}
            library.data['jobs'][key] = old
            library.save()
            restored = Library(root/'data')
            self.assertEqual(len(restored.records()[0]['versions']), 1)
            self.assertIsNone(restored.next_job(root/'new-texts', True))
            restored.data['jobs'][key].update(status='pending')
            restored.save()
            again = Library(root/'data')
            self.assertEqual(again.data['jobs'][key]['status'], 'cancelled')
            self.assertEqual(len(again.records()[0]['versions']), 1)
            self.assertEqual(result.read_text(encoding='utf-8'), '原结果')

    def test_batch_jobs_freeze_vocabulary_and_deduplicate_active_submissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pipeline = AutoPipeline(root/'data', lambda _:None)
            device = pipeline.library.register(make_receipt(root/'device', content=b'device'))
            source = Path(make_receipt(root/'source')['path'])
            pipeline.vocabulary.add(['青岚'], '测试')
            snapshot = pipeline.vocabulary.snapshot(['凌川'])
            successful, errors = submit_local_batch(pipeline, [source, source, root/'missing.wav'],
                root/'imports', snapshot, '示例项目', lambda _:None)
            self.assertEqual(len(successful), 2)
            self.assertEqual(len(errors), 1)
            jobs = pipeline.library.data['jobs']
            self.assertEqual(len(jobs), 2)  # Original device job and one local job.
            key, job = pipeline.library.next_job(root/'texts', allow_auto=False)
            self.assertNotEqual(key, device)
            self.assertEqual(job['kind'], 'local')
            self.assertEqual(job['project'], '示例项目')
            self.assertEqual(job['vocabulary']['general'], ['青岚'])
            self.assertEqual(job['vocabulary']['special'], ['凌川'])
            pipeline.vocabulary.add(['李沐'], '测试')
            self.assertEqual(jobs[key]['vocabulary']['general'], ['青岚'])
            pipeline.library.update(key, status='running')
            restored = Library(root/'data')
            self.assertEqual(restored.next_job(root/'texts', False)[0], key)
            restored.cancel(key)
            self.assertEqual(restored.next_job(root/'texts', True)[0], device)

    def test_local_transcription_finishes_outside_schedule_and_retains_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pipeline = AutoPipeline(root/'data', lambda _:None)
            source = Path(make_receipt(root/'source')['path'])
            update_settings(root/'data', auto_transcribe=False, schedule_start='00:00', schedule_end='00:01')
            for index in range(2):
                successful, errors = submit_local_batch(pipeline, [source], root/'imports',
                    pipeline.vocabulary.snapshot(['青岚']), '示例项目', lambda _:None)
                self.assertFalse(errors)
                key, job = pipeline.library.next_job(root/'texts', False)
                pipeline.library.update(key, status='running')
                pipeline.process = SimpleNamespace(stdin=io.StringIO())
                pipeline.output_lines.put({'event':'result','segments':[{'start':0,'end':2,'text':f'结果 {index}'}]})
                with patch.object(pipeline, '_start_process'):
                    pipeline.transcribe(key, job, root/'texts')
            record = pipeline.library.records()[0]
            self.assertEqual(len(record['versions']), 2)
            self.assertTrue(all(v['label']=='本地音频转写' for v in record['versions']))
            self.assertIn('结果 0', Path(record['versions'][0]['output']).read_text(encoding='utf-8-sig'))
            self.assertIn('结果 1', Path(record['versions'][1]['output']).read_text(encoding='utf-8-sig'))
            self.assertIsNone(Library(root/'data').next_job(root/'texts', True))

    def test_invalid_input_does_not_create_job_and_shutdown_stops_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pipeline = AutoPipeline(root, lambda _:None)
            path = root/'unsupported.m4a'; path.write_bytes(b'unsupported')
            snapshot = pipeline.vocabulary.snapshot()
            success, errors = submit_local_batch(pipeline, [path], root/'imports', snapshot, '', lambda _:None)
            self.assertFalse(success); self.assertEqual(len(errors), 1)
            source = Path(make_receipt(root/'source')['path'])
            original = pipeline.library.import_audio
            def stop_after_copy(*args):
                result = original(*args)
                pipeline.stop_event.set()
                return result
            with patch.object(pipeline.library, 'import_audio', side_effect=stop_after_copy):
                submit_local_batch(pipeline, [source], root/'imports', snapshot, '', lambda _:None)
            self.assertEqual(pipeline.library.data['jobs'], {})
            self.assertTrue(Path(pipeline.library.records()[0]['receipt']['path']).exists())

    def test_archive_location_separates_imports_and_honors_explicit_setting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'数据'
            self.assertEqual(imported_audio_folder(root), root.parent/'录音'/'导入录音')
            update_settings(root, import_dir=str(Path(folder)/'custom'))
            self.assertEqual(imported_audio_folder(root), Path(folder)/'custom')

    def test_dedicated_panel_waits_for_start_and_submits_project_and_terms(self):
        from soundcore_microphone import tk, App
        from local_transcription_panel import LocalTranscriptionPanel
        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk(); root.withdraw()
            pipeline = AutoPipeline(Path(folder)/'data', lambda _:None)
            app = SimpleNamespace(root=root, pipeline=pipeline, closing=False, transcription_panel=None,
                show_library=Mock(), show_vocabulary=Mock(), show_sync_panel=Mock())
            panel = LocalTranscriptionPanel(app, Path(folder)/'data'); app.transcription_panel=panel
            try:
                source = Path(make_receipt(Path(folder)/'source')['path'])
                panel.add_files([source, source])
                panel.project.set('示例项目'); panel.terms.insert('1.0', '青岚\n青岚\nsoundcore Work')
                root.update()
                self.assertEqual(len(panel.paths), 1)
                self.assertEqual(pipeline.library.data['jobs'], {})
                self.assertFalse(pipeline.stop_event.is_set())
                panel.start_button.invoke()
                panel.worker.join(10)
                self.assertFalse(panel.worker.is_alive())
                panel.poll()
                self.assertFalse(panel.busy); self.assertEqual(panel.paths, [])
                key, job = pipeline.library.next_job(folder, False)
                self.assertEqual(job['project'], '示例项目')
                self.assertEqual(job['vocabulary']['special'], ['青岚','soundcore Work'])
                self.assertEqual(job['kind'], 'local')
                target = SimpleNamespace(show_transcription=Mock())
                App.dispatch(target, 'transcribe')
                target.show_transcription.assert_called_once()
            finally:
                panel.close(); root.destroy()


if __name__ == '__main__': unittest.main()

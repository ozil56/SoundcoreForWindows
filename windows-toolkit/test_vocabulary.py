import copy
import json
from pathlib import Path
import tempfile
import io
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from auto_sync import AutoPipeline,Library
from vocabulary import VocabularyStore,parse_terms
from vocabulary_context import ContextSelector
from test_auto_sync import make_receipt
from tool_settings import update_settings


class VocabularyTests(unittest.TestCase):
    def test_shutdown_during_worker_read_is_recoverable_interruption(self):
        with tempfile.TemporaryDirectory() as root:
            pipeline=AutoPipeline(root,lambda _:None)
            base=pipeline.library.register(make_receipt(root))
            key=pipeline.library.queue_manual(base,pipeline.vocabulary.snapshot())
            job=copy.deepcopy(pipeline.library.data['jobs'][key])
            pipeline.process=SimpleNamespace(stdin=io.StringIO())
            def closed(timeout=None):
                pipeline.stop_event.set()
                return {'event':'closed'}
            with patch.object(pipeline,'_start_process'),patch.object(pipeline.output_lines,'get',side_effect=closed):
                with self.assertRaises(InterruptedError): pipeline.transcribe(key,job,root)

    def test_manual_runs_outside_schedule_and_auto_yields_at_checkpoint_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            pipeline=AutoPipeline(root,lambda _:None)
            receipt=make_receipt(root); base=pipeline.library.register(receipt)
            manual=pipeline.library.queue_manual(base,pipeline.vocabulary.snapshot(['青岚']))
            job=copy.deepcopy(pipeline.library.data['jobs'][manual])
            pipeline.library.update(manual,status='running')
            pipeline.process=SimpleNamespace(stdin=io.StringIO())
            pipeline.output_lines.put({'event':'result','segments':[{'start':0,'end':2,'text':'真实测试结果'}]})
            update_settings(root,auto_transcribe=False,schedule_enabled=True,schedule_start='01:00',schedule_end='01:01')
            with patch.object(pipeline,'_start_process'):
                pipeline.transcribe(manual,job,Path(root)/'texts')
            self.assertEqual(pipeline.library.data['jobs'][manual]['status'],'complete')
            queued=pipeline.library.queue_manual(base,pipeline.vocabulary.snapshot())
            update_settings(root,auto_transcribe=True,schedule_enabled=False)
            pipeline.output_lines.put({'event':'progress','percent':12,'segment_boundary':True})
            with patch.object(pipeline,'_start_process'),patch.object(pipeline,'_close_process') as close:
                with self.assertRaises(InterruptedError):
                    pipeline.transcribe(base,copy.deepcopy(pipeline.library.data['jobs'][base]),root)
                close.assert_called_once()
            self.assertEqual(pipeline.library.next_job(root,False)[0],queued)

    def test_import_review_snapshot_and_undo_do_not_contaminate_old_jobs(self):
        with tempfile.TemporaryDirectory() as root:
            store=VocabularyStore(root)
            self.assertEqual(parse_terms('\ufeff 青岚\n\n soundcore Work \n青岚'),['青岚','soundcore Work'])
            store.add(['青岚'],'手动确认')
            store.add(['李沐'],'录音纠正','pending','李专家')
            old=store.snapshot(['凌川'])
            self.assertEqual(old['general'],['青岚'])
            row=store.rows('pending')[0]
            store.review([row['id']],'accepted')
            self.assertIn('李沐',store.snapshot()['general'])
            self.assertEqual(old['general'],['青岚'])
            store.undo()
            self.assertEqual(store.rows('pending')[0]['term'],'李沐')
            self.assertEqual(VocabularyStore(root).snapshot()['general'],['青岚'])
            self.assertEqual((store.folder/'快照'/(old['sha256']+'.json')).read_text(encoding='utf-8'),
                json.dumps(old,ensure_ascii=False,indent=2))

    def test_large_dictionary_search_includes_late_homophones_with_bounded_prompt(self):
        selector=ContextSelector({'general':['测试词'+str(i) for i in range(1200)]+['青岚','凌川'],'special':['李沐']})
        terms=selector.select('清蓝高速的施工图，请凌穿专家发言。')
        self.assertIn('青岚',terms); self.assertIn('凌川',terms)
        self.assertLessEqual(len(terms),70); self.assertLessEqual(len('、'.join(terms)),1100)

    def test_manual_priority_cancellation_restart_and_version_preservation(self):
        with tempfile.TemporaryDirectory() as root:
            library=Library(root); receipt=make_receipt(root); base=library.register(receipt)
            old=Path(root)/'old.txt'; old.write_text('旧结果',encoding='utf-8')
            library.finish_version(base,old,library.data['jobs'][base],root)
            original=copy.deepcopy(library.records()[0]['versions'][0])
            library.edit_record([base],project='测试项目')
            snapshot=VocabularyStore(root).snapshot(['青岚'])
            manual=library.queue_manual(base,snapshot)
            self.assertEqual(library.next_job(root,allow_auto=False)[0],manual)
            library.update(manual,status='running')
            restored=Library(root)
            self.assertEqual(restored.next_job(root,False)[0],manual)
            new=Path(root)/'new.txt'; new.write_text('新结果',encoding='utf-8')
            restored.finish_version(manual,new,restored.data['jobs'][manual],root)
            record=restored.records()[0]
            self.assertEqual(record['versions'][0],original)
            self.assertEqual(len(record['versions']),2)
            restored.select_version(base,original['id'])
            self.assertEqual(restored.records()[0]['current_version'],original['id'])
            self.assertEqual(old.read_text(encoding='utf-8'),'旧结果')
            cancelled=restored.queue_manual(base,snapshot); restored.cancel(cancelled)
            self.assertIsNone(restored.next_job(root,False))

    def test_recording_panel_does_not_pause_bridge_and_keeps_version_selection(self):
        from soundcore_microphone import tk
        from local_library_panel import LocalLibraryPanel
        from vocabulary_panel import VocabularyPanel
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk(); root.withdraw()
            pipeline=AutoPipeline(folder,lambda _:None)
            key=pipeline.library.register(make_receipt(folder))
            app=SimpleNamespace(root=root,pipeline=pipeline,library_panel=None,vocabulary_panel=None,
                show_manager=lambda *_:self.fail('Local library must not open BLE manager'),show_vocabulary=lambda:None)
            panel=LocalLibraryPanel(app,folder); app.library_panel=panel
            vocab=VocabularyPanel(app,folder); app.vocabulary_panel=vocab
            try:
                root.update(); panel.tree.selection_set(key); panel.details()
                self.assertEqual(panel.selected()['id'],key)
                self.assertGreater(panel.window.winfo_width(),850)
                self.assertFalse(pipeline.stop_event.is_set())
            finally: panel.close(); vocab.close(); root.destroy()


if __name__=='__main__': unittest.main()

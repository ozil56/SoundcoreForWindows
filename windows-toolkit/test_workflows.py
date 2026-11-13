import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from asr_worker import adaptive_decode,OutputLimit,repeating_tokens
from auto_sync import AutoPipeline,EnrichmentControl
from vocabulary import ProjectVocabularies,VocabularyStore
from ollama_summary import enrich,safe_title,render_minutes
from tool_settings import update_settings,write_json,read_settings
from test_auto_sync import make_receipt
from workflow_panels import automatic_summary


class WorkflowTests(unittest.TestCase):
    def test_loop_guard_only_rejects_long_repetitive_output(self):
        self.assertTrue(repeating_tokens([1,2,3,4,5,6,7,8]*6))
        self.assertFalse(repeating_tokens(list(range(160))))
        self.assertFalse(repeating_tokens([1,2]*4))
    def test_adaptive_decoder_never_returns_truncated_text_or_skips_audio(self):
        seen=[]
        def decode(a,b,retry):
            if b-a>4*16000:raise OutputLimit('cap')
            seen.append((a,b));return f'{a}-{b}'
        text,retries=adaptive_decode(0,20*16000,decode)
        self.assertGreater(retries,0)
        self.assertEqual(seen[0][0],0);self.assertEqual(seen[-1][1],20*16000)
        self.assertTrue(all(left[1]==right[0] for left,right in zip(seen,seen[1:])))
        self.assertEqual(len(text.split()),len(seen))
        with self.assertRaises(OutputLimit):adaptive_decode(0,16000,lambda *_:(_ for _ in ()).throw(OutputLimit('still capped')))

    def test_short_loop_uses_bounded_retry(self):
        calls=[]
        def decode(a,b,retry):
            calls.append(retry)
            if not retry:raise OutputLimit()
            return '恢复结果'
        self.assertEqual(adaptive_decode(0,16000,decode),('恢复结果',1))
        self.assertEqual(calls,[False,True])

    def test_project_dictionaries_optional_general_and_recoverable_deletion(self):
        with tempfile.TemporaryDirectory() as folder:
            general=VocabularyStore(folder);general.add(['通用词'],'test')
            projects=ProjectVocabularies(folder);key=projects.add('项目甲')
            store=projects.store(key);store.add(['术语甲','术语乙'],'test')
            term_id=store.rows()[0]['id'];store.review([term_id],'disabled')
            self.assertEqual(projects.terms(key),['术语乙'])
            snapshot=general.snapshot(projects.terms(key),include_general=False)
            self.assertEqual(snapshot['general'],[])
            projects.update(key,state='deleted')
            with self.assertRaises(ValueError):projects.terms(key)
            projects.update(key,state='accepted',name='项目乙')
            self.assertEqual(projects.terms(key),['术语乙'])
            restored=ProjectVocabularies(folder)
            self.assertEqual(restored.rows()[0]['name'],'项目乙')
            self.assertEqual(snapshot['special'],['术语乙'])

    def make_output(self,folder,seconds=180,text=None):
        pipeline=AutoPipeline(folder,lambda _:None);receipt=make_receipt(folder);receipt['seconds']=seconds
        return pipeline.publish_text(receipt,{'segments':[{'start':0,'end':seconds,'text':text or '项目讨论内容。'*30}],
            'audio_seconds':seconds},Path(folder)/'text')

    def test_short_recording_never_calls_ollama(self):
        with tempfile.TemporaryDirectory() as folder:
            output=self.make_output(folder,119)
            with patch('ollama_summary.chat') as chat:
                result=enrich(output,{},lambda *_:None)
            self.assertEqual(result['state'],'skipped');chat.assert_not_called()
            self.assertTrue(output.exists())

    def test_minutes_polish_all_chunks_and_preserve_original_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            text='第一部分具体内容。'*350+'第二部分具体内容。'*350
            output=self.make_output(folder,text=text);original=output.read_bytes();parts=[]
            def chat(settings,system,text,schema,limit=6000):
                if 'p0' in schema['properties']:
                    paragraphs=json.loads(text)['paragraphs']
                    parts.extend(paragraphs.values());return dict(paragraphs,notes='项目讨论和明确待办')
                return {'title':'项目/会议:审查','main':'主要内容','todos':'无明确待办事项'}
            with patch('ollama_summary.chat',side_effect=chat):
                result=enrich(output,{},lambda *_:None)
                again=enrich(output,{},lambda *_:None)
            self.assertEqual(result['state'],'complete')
            self.assertEqual(''.join(p.replace('\n','') for p in parts),text)
            self.assertEqual(output.read_bytes(),original)
            self.assertEqual(again['minutes_output'],result['minutes_output'])
            self.assertNotIn('/',Path(result['named_output']).name)
            minutes=Path(result['minutes_output']).read_text(encoding='utf-8-sig')
            self.assertEqual(Path(result['minutes_output']).suffix,'.md')
            self.assertTrue(minutes.startswith('# '+result['title']+'\n\n'))
            self.assertEqual(Path(result['named_output']).suffix,'.txt')
            for title in ('主要内容','待办事项','具体完整内容的润色'):self.assertIn('\n\n## '+title+'\n\n',minutes)
            self.assertTrue(Path(result['minutes_output']+'.json').is_file())

    def test_markdown_minutes_keep_details_and_format_lists(self):
        text=render_minutes('项目审查','1. 保留 3.5 米净宽；2、下一阶段复核\n- 已确认方案',
            '周五提交材料；负责人待确认',['正文第一段：宽度 3.5 米。','正文第二段保持原文。'])
        self.assertIn('- 保留 3.5 米净宽\n- 下一阶段复核\n- 已确认方案',text)
        self.assertIn('## 待办事项\n\n- 周五提交材料\n- 负责人待确认',text)
        self.assertIn('正文第一段：宽度 3.5 米。\n\n正文第二段保持原文。',text)
        empty=render_minutes('测试','内容','无明确待办事项。',['正文'])
        self.assertIn('## 待办事项\n\n无明确待办事项。',empty)

    def test_failed_minutes_keep_transcript_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            output=self.make_output(folder);original=output.read_bytes()
            with patch('ollama_summary.chat',side_effect=TimeoutError('offline')):
                with self.assertRaises(TimeoutError):enrich(output,{},lambda *_:None)
            self.assertEqual(output.read_bytes(),original)

    def test_overview_deduplicates_recordings_and_computes_next_window(self):
        with tempfile.TemporaryDirectory() as folder:
            pipeline=AutoPipeline(folder,lambda _:None)
            key=pipeline.library.register(make_receipt(folder))
            second=pipeline.library.queue_manual(key,pipeline.vocabulary.snapshot())
            settings={'auto_sync':True,'auto_transcribe':True,'schedule_start':'20:00','schedule_end':'08:00'}
            state=automatic_summary(pipeline,settings,dt.datetime(2026,11,13,12,tzinfo=dt.timezone.utc))
            self.assertEqual(state['local'],1);self.assertEqual(state['tasks'],2)
            self.assertIn('立即',state['expected'])
            pipeline.library.cancel(second)
            self.assertIn('11-13 20:00',automatic_summary(pipeline,settings,dt.datetime(2026,11,13,12,tzinfo=dt.timezone.utc))['expected'])

    def test_changing_destination_does_not_retranscribe_completed_history(self):
        with tempfile.TemporaryDirectory() as folder:
            pipeline=AutoPipeline(folder,lambda _:None)
            key=pipeline.library.register(make_receipt(folder))
            pipeline.library.update(key,status='complete',output_root=str(Path(folder)/'old'))
            self.assertIsNone(pipeline.library.next_job(Path(folder)/'new',True))

    def test_minutes_yield_to_schedule_changes_and_new_asr_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            pipeline=AutoPipeline(folder,lambda _:None)
            key=pipeline.library.register(make_receipt(folder))
            pipeline.library.update(key,status='complete')
            update_settings(folder,ollama_enabled=True,auto_transcribe=True,schedule_enabled=False)
            control=EnrichmentControl(pipeline,key)
            self.assertFalse(control.is_set())
            update_settings(folder,auto_transcribe=False)
            self.assertTrue(control.is_set())
            pipeline.library.update(key,run_immediately=True)
            self.assertFalse(control.is_set())
            pipeline.library.queue_manual(key,pipeline.vocabulary.snapshot())
            self.assertTrue(control.is_set())

    def test_new_panels_use_seven_entries_and_self_service_snapshot(self):
        from soundcore_microphone import tk
        from tray_app import MENU_ITEMS
        from self_service_panel import SelfServicePanel
        from dictionary_panel import DictionaryPanel
        from settings_panel import SettingsPanel
        from workflow_panels import AutomaticPanel
        self.assertEqual([label for label,_ in MENU_ITEMS],['连接录音豆','设备录音管理','自动转写','自助转写','词库管理','设置','退出'])
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();pipeline=AutoPipeline(Path(folder)/'data',lambda _:None)
            app=SimpleNamespace(root=root,pipeline=pipeline,closing=False,version='test',transcription_panel=None,
                vocabulary_panel=None,automatic_panel=None,settings_panel=None,autostart=Mock(enabled=Mock(return_value=False)),
                sync_device_now=Mock(),service=Mock(snapshot=Mock(return_value={'phase':'waiting'})))
            panels=[]
            try:
                for factory in (SelfServicePanel,DictionaryPanel,SettingsPanel,AutomaticPanel):
                    panel=factory(app,Path(folder)/'data');panel.window.withdraw();panels.append(panel)
                panel=panels[0]
                source=Path(make_receipt(Path(folder)/'source')['path'])
                panel.add_files([source]);panel.general.set(False)
                with patch.object(pipeline,'runtime'):panel.start()
                panel.worker.join(5);panel.poll()
                self.assertFalse(panel.busy)
                task=pipeline.library.next_job(folder,False)
                self.assertEqual(task[1]['vocabulary']['general'],[])
                self.assertEqual(task[1]['vocabulary']['special'],[])
                root.update()
            finally:
                for panel in panels:panel.close()
                root.destroy()


if __name__=='__main__':unittest.main()

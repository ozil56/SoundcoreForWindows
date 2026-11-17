import copy
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from meeting_actions import number_key, unsupported_numbers, number_claims, review_actions, validate_actions, grounded_fields, extract_actions
from ollama_summary import OllamaOutputLimit, conservative_overview, overview_issues, overview_followups, enrich
from tool_settings import write_json
import test_meeting_actions as fixtures


class MinutesValidationTests(unittest.TestCase):
    def test_numeric_units_are_grounded_without_guessing(self):
        self.assertEqual(unsupported_numbers('安排6人，面积123平方米','六名，面积一百二十三平米'),[])
        self.assertEqual(unsupported_numbers('400个车位','四百'),['400个车位'])
        self.assertEqual(unsupported_numbers('750个车位','七百五个车位'),['750个车位'])
        self.assertEqual(unsupported_numbers('13000人','一万三人'),['13000人'])
        self.assertEqual(unsupported_numbers('60000元','六万元'),[])
        self.assertEqual(unsupported_numbers('750万元','七百五万元'),['750万元'])
        self.assertEqual(unsupported_numbers('705万元','七百五万元'),['705万元'])
        self.assertEqual(unsupported_numbers('七百五万元','七百五万元'),[])
        self.assertEqual(number_key('一千零三'),'1003')

    def reviewed(self,chat,items=None):
        fixture=fixtures.MeetingActionTests();segments=fixture.segments
        items=items or validate_actions({'items':[fixture.item()]},segments,{0})
        return review_actions(items,segments,{}, {},chat,lambda:None,lambda:None,lambda *_:None)

    def verdict(self,**changes):
        return dict(dict(id=0,verdict='明确安排',task='比较路线方案',details='比较路线方案并统计工程量',
                         deliverable='对比表',reason='原文明示要求'),**changes)

    def test_review_is_independent_and_uses_original_segments(self):
        chat=Mock(return_value={'reviews':[self.verdict()]});self.reviewed(chat)
        payload=json.loads(chat.call_args.args[2]);candidate=payload['candidates'][0]
        self.assertEqual(set(candidate),{'id','topic','source_segments'})
        self.assertEqual(payload['source'][0]['text'],fixtures.MeetingActionTests.segments[0]['text'])

    def test_missing_duplicate_blank_and_truncated_reviews_retry_then_fail(self):
        invalids=[{'reviews':[]},{'reviews':[self.verdict(),self.verdict()]},
                  {'reviews':[self.verdict(details=' ')]},{'reviews':[self.verdict(reason=' ')]},
                  OllamaOutputLimit('length')]
        for invalid in invalids:
            chat=Mock(side_effect=[invalid,{'reviews':[self.verdict()]}])
            self.assertEqual(len(self.reviewed(chat)),1);self.assertEqual(chat.call_count,2)
        chat=Mock(return_value={'reviews':[]})
        with self.assertRaisesRegex(ValueError,'不完整'):self.reviewed(chat)
        self.assertEqual(chat.call_count,2)

    def test_numeric_failure_keeps_evidence_with_conservative_text(self):
        chat=Mock(return_value={'reviews':[self.verdict(task='安排400个车位',details='建设400个车位') ]})
        result=self.reviewed(chat)[0]
        self.assertEqual(chat.call_count,2);self.assertNotIn('400',result['task'])
        self.assertNotIn('400',result['details']);self.assertTrue(result['evidence'])

    def test_suggestion_is_not_promoted_and_completed_is_removed(self):
        chat=Mock(return_value={'reviews':[self.verdict(verdict='建议待确认')]})
        self.assertEqual(self.reviewed(chat)[0]['kind'],'建议待确认')
        self.assertEqual(self.reviewed(Mock(return_value={'reviews':[self.verdict(verdict='不列为待办')]})),[])

    def test_merged_deadline_must_still_be_grounded(self):
        fixture=fixtures.MeetingActionTests();item=fixture.item()
        item['deadline']='周五前；下周一'
        grounded=grounded_fields(item)
        self.assertEqual(grounded['deadline'],'');self.assertEqual(grounded['deadline_type'],'未明确')

    def test_omitted_tentative_deadline_qualifier_is_restored_from_quote(self):
        fixture=fixtures.MeetingActionTests()
        for quote,deadline,expected in [
            ('请小王完成方案，有可能在春节之前提交。','春节之前','有可能在春节之前'),
            ('请小王争取周五前完成方案。','周五前','争取周五前'),
            ('请小王周五前完成方案。','周五前','周五前')]:
            item=fixture.item();item.update(deadline=deadline,evidence=[dict(segment=0,quote=quote)])
            result=validate_actions({'items':[item]},[dict(start=0,end=10,text=quote)],{0})[0]
            self.assertEqual(result['deadline'],expected)
            self.assertEqual(result['deadline_type'],'目标时点' if deadline!=expected else '明确期限')

    def test_tentative_qualifier_cannot_cross_two_quotes(self):
        item=fixtures.MeetingActionTests().item();item['evidence']=[dict(quote='这件事我们争取'),dict(quote='另一个事项周五前完成')]
        result=grounded_fields(item)
        self.assertEqual(result['deadline'],'周五前');self.assertEqual(result['deadline_type'],'明确期限')

    def test_merged_citations_and_review_sources_stay_within_four(self):
        fixture=fixtures.MeetingActionTests()
        segments=[dict(start=i*10,end=(i+1)*10,text=f'请小王周五前完成路线方案对比，采用资料编号{i}。') for i in range(7)]
        items=[]
        for indexes in (range(4),range(3,7)):
            item=fixture.item();item['evidence']=[dict(segment=i,quote=segments[i]['text']) for i in indexes]
            items.append(item)
        chat=Mock(side_effect=[{'items':items},{'groups':[[0,1]]},{'reviews':[self.verdict()]}])
        result=extract_actions(segments,{}, {},chat,lambda:None,lambda:None,lambda *_:None,OllamaOutputLimit)
        self.assertEqual(len(result),1);self.assertEqual(len(result[0]['evidence']),4)
        review=json.loads(chat.call_args.args[2])
        self.assertEqual({s['segment'] for s in review['source']},{e['segment'] for e in result[0]['evidence']})

    def test_overview_fallback_checks_topic_and_body(self):
        main='建设400个车位：需完善停车场。\n场地情况：介绍地块条件；讨论入口位置。'
        result=conservative_overview(main,'介绍地块条件和入口位置')
        self.assertFalse(any(overview_issues(result,'介绍地块条件和入口位置').values()))
        self.assertIn('请结合原文确认',result)

    def test_overview_rejects_execution_sections_and_unqualified_commands(self):
        for main in ('后续安排：梳理专业接口并联系供应方。',
                     '安全机制：明确源代码需保存在内部。',
                     '技术路径：交流系统架构，并计划用外部软件复核。',
                     '技术路径：介绍现状，并计划使用外部软件复核。',
                     '数据：讨论接口，要求外部单位提供新表格。',
                     '宣传：讨论宣传策略，制定具体落实方案。'):
            self.assertTrue(any(overview_issues(main,main).values()))
            fallback=conservative_overview(main,main)
            self.assertFalse(any(overview_issues(fallback,main).values()))
            self.assertNotIn('梳理专业接口',fallback)
        main='安全机制：介绍源代码内部保存与节点本地运行的边界；讨论接口需求。'
        self.assertFalse(any(overview_issues(main,main).values()))

    def test_numeric_alternatives_cannot_be_rewritten_as_a_range(self):
        main='规模：介绍100-200条流程。'
        self.assertTrue(overview_issues(main,'一百条流程或者两百条流程')['unsupported_ranges'])
        self.assertFalse(overview_issues(main,'一百至两百条流程')['unsupported_ranges'])
        self.assertFalse(overview_issues('人员：介绍1–2人参与。','一到两名参与')['unsupported_ranges'])

    def test_conservative_overview_preserves_valid_clauses_and_omits_task_sections(self):
        main='数据格式：介绍现有接口以文件交互，要求供应商提交统计表。\n近期待办：介绍准备近期提交材料。'
        source='现有接口以文件交互'
        result=conservative_overview(main,source)
        self.assertIn('介绍现有接口以文件交互',result)
        self.assertIn('部分口径待核实',result)
        self.assertNotIn('供应商',result);self.assertNotIn('待办',result)
        self.assertFalse(any(overview_issues(result,source).values()))

    def make_output(self,folder):
        output=Path(folder)/'source.txt';output.write_text('原始转写保持',encoding='utf-8')
        text='讨论路线设计的条件与边界，现场已经解释了测量资料。'*80
        write_json(Path(str(output)+'.json'),dict(segments=[dict(start=0,end=180,text=text)],
                   source={'sha256':'synthetic','seconds':180},recorded_at='2026-11-16T01:00:00+08:00'))
        return output

    def fake_chat(self,settings,system,text,schema,limit=3000):
        if 'p0' in schema['properties']:return dict(json.loads(text)['paragraphs'],notes='路线设计条件讨论')
        if 'items' in schema['properties']:return {'items':[]}
        return {'title':'路线讨论','main':'路线条件：讨论路线设计的边界和测量资料。'}

    def test_overview_version_change_keeps_body_and_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            output=self.make_output(folder)
            with patch('ollama_summary.chat',side_effect=self.fake_chat) as chat:
                enrich(output,{},lambda *_:None)
                before=chat.call_count
                with patch('ollama_summary.OVERVIEW_VERSION','style-only-next'):
                    enrich(output,{},lambda *_:None)
                self.assertEqual(chat.call_count,before+1)
                self.assertIn('main',chat.call_args.args[3]['properties'])

    def test_legacy_auto_context_migration_preserves_polished_body(self):
        with tempfile.TemporaryDirectory() as folder:
            output=self.make_output(folder);settings={'ollama_model':'chosen-local-model'}
            with patch('ollama_summary.chat',side_effect=self.fake_chat) as chat:
                enrich(output,settings,lambda *_:None)
                checkpoint=Path(str(output)+'.minutes-progress.json');state=json.loads(checkpoint.read_text(encoding='utf-8'))
                meta=json.loads(Path(str(output)+'.json').read_text(encoding='utf-8'))
                text=meta['segments'][0]['text']
                state['signature']=hashlib.sha256(('paragraphs-v3:'+text+json.dumps({k:settings.get(k) for k in ('ollama_url','ollama_model')},sort_keys=True)).encode()).hexdigest()
                write_json(checkpoint,state);before=chat.call_count
                enrich(output,settings,lambda *_:None)
                self.assertEqual(chat.call_count,before)

    def test_actions_change_invalidates_overview(self):
        with tempfile.TemporaryDirectory() as folder:
            output=self.make_output(folder)
            fixture=fixtures.MeetingActionTests()
            action=validate_actions({'items':[fixture.item()]},fixture.segments,{0})
            with patch('ollama_summary.chat',side_effect=self.fake_chat) as chat:
                with patch('ollama_summary.extract_actions',return_value=[]):enrich(output,{},lambda *_:None)
                before=chat.call_count
                with patch('ollama_summary.extract_actions',return_value=action):enrich(output,{},lambda *_:None)
                self.assertEqual(chat.call_count,before+1)
                payload=json.loads(chat.call_args.args[2]);self.assertEqual(payload['verified_followups'],overview_followups(action))

    def test_overview_context_compaction_preserves_quotes_and_tentative_deadline(self):
        fixture=fixtures.MeetingActionTests();item=fixture.item()
        result=overview_followups([item])[0]
        self.assertEqual(result['evidence'],[e['quote'] for e in item['evidence']])
        self.assertEqual(result['deadline'],item['deadline'])
        self.assertEqual(result['deadline_type'],item['deadline_type'])
        self.assertNotIn('owner',result)

    def test_overview_truncation_is_bounded_and_never_cached_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            output=self.make_output(folder);attempts=[]
            def chat(*args,**kwargs):
                if 'title' in args[3]['properties']:
                    attempts.append(1);raise OllamaOutputLimit('truncated')
                return self.fake_chat(*args,**kwargs)
            with patch('ollama_summary.chat',side_effect=chat):
                with self.assertRaisesRegex(ValueError,'三次不完整'):enrich(output,{},lambda *_:None)
            self.assertEqual(len(attempts),3)
            state=json.loads(Path(str(output)+'.minutes-progress.json').read_text(encoding='utf-8'))
            self.assertTrue(state['parts']);self.assertNotIn('overview',state)

    def test_single_long_paragraph_truncation_preserves_full_text(self):
        with tempfile.TemporaryDirectory() as folder:
            output=self.make_output(folder)
            def chat(*args,**kwargs):
                if 'p0' in args[3]['properties']:raise OllamaOutputLimit('truncated')
                return self.fake_chat(*args,**kwargs)
            with patch('ollama_summary.chat',side_effect=chat):result=enrich(output,{},lambda *_:None)
            state=json.loads(Path(str(output)+'.minutes-progress.json').read_text(encoding='utf-8'))
            full=''.join(part['polished'].replace('\n','') for part in state['parts'])
            meta=json.loads(Path(str(output)+'.json').read_text(encoding='utf-8'))
            self.assertEqual(full,meta['segments'][0]['text'])
            self.assertGreater(result['verbatim_fallbacks'],0)


if __name__=='__main__':unittest.main()

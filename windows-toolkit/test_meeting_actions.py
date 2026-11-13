import copy
import unittest
from unittest.mock import Mock

from meeting_actions import validate_actions,extract_actions,render_actions,action_chunks
from ollama_summary import OllamaOutputLimit


class MeetingActionTests(unittest.TestCase):
    segments=[{'start':65,'end':88,'text':'请小王周五前完成两条路线方案对比，提交含工程量的对比表，先等测量资料到齐。'}]

    def item(self):
        return {'task':'完成两条路线方案对比','kind':'明确安排','details':'比较两条路线方案并统计工程量',
            'deliverable':'含工程量的对比表','owner':'小王','deadline':'周五前','dependencies':'测量资料到齐',
            'evidence':[{'segment':0,'quote':self.segments[0]['text']}]}

    def test_evidence_must_be_verbatim_and_owner_is_not_invented(self):
        item=self.item();item['owner']='小李';item['deadline']='本月底'
        result=validate_actions({'items':[item]},self.segments,{0})[0]
        self.assertEqual(result['owner'],'');self.assertEqual(result['deadline'],'')
        item['evidence'][0]['quote']='未出现过的任务安排'
        with self.assertRaisesRegex(ValueError,'不匹配'):validate_actions({'items':[item]},self.segments,{0})

    def test_checkpoint_reuse_and_incomplete_merge_never_drop_tasks(self):
        state={};save=Mock();second=self.item();second['task']='复核方案工程量'
        chat=Mock(side_effect=[{'items':[self.item(),second]},{'groups':[[0]]}])
        first=extract_actions(self.segments,{},state,chat,save,lambda:None,lambda *_:None,OllamaOutputLimit)
        self.assertEqual(len(first),2)
        again=extract_actions(self.segments,{},state,chat,save,lambda:None,lambda *_:None,OllamaOutputLimit)
        self.assertEqual(first,again);self.assertEqual(chat.call_count,2)
        rendered=render_actions(first)
        for detail in ('小王','周五前','对比表','测量资料到齐','00:01:05–00:01:28','原文依据'):
            self.assertIn(detail,rendered)

    def test_new_model_invalidates_action_cache(self):
        state={};chat=Mock(return_value={'items':[]})
        for model in ('model-a','model-b'):
            extract_actions(self.segments,{'ollama_model':model},state,chat,lambda:None,lambda:None,lambda *_:None,OllamaOutputLimit)
        self.assertEqual(chat.call_count,2)

    def test_groups_cover_all_source_segments_and_keep_boundary_context(self):
        segments=[dict(start=i,end=i+1,text='文'*700) for i in range(20)]
        chunks=action_chunks(segments)
        self.assertEqual({u['segment'] for g in chunks for u in g},set(range(20)))
        for left,right in zip(chunks,chunks[1:]):self.assertEqual(left[-1],right[0])


if __name__=='__main__':unittest.main()

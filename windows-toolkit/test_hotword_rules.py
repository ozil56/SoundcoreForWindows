"""Full-library retrieval and acoustic-first integration; no real model required."""
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from vocabulary import parse_terms
from vocabulary_context import ContextSelector, _NearSounds, STRATEGY, MAX_TERMS, MAX_CHARS


class FullVocabularyTests(unittest.TestCase):
    def test_empty_unrelated_and_small_library_do_not_seed(self):
        selector=ContextSelector({'special':['范昭平'],'general':['沥青混凝土'],'unselected':['天气晴朗']})
        for text in ('','，。！？','今天天气晴朗','范招兵'):
            self.assertEqual(selector.select(text),[])
        self.assertEqual(selector.select('范招平'),['范昭平'])

    def test_tail_of_over_5000_terms_and_selected_only(self):
        selector=ContextSelector({'general':['unrelated_identifier_'+str(i) for i in range(6000)]+['范昭平'],
                                  'unselected':['桥梁工程']})
        self.assertEqual(len(selector.terms),6001)
        self.assertEqual(selector.select('请范招平发言，桥梁工程'),['范昭平'])

    def test_import_normalization_dedup_and_dictionary_order(self):
        self.assertEqual(parse_terms('\ufeff ＡＢＣ\n abc \n\ufeff\n\ufeff 范昭平\n'),['ABC','范昭平'])
        selected=ContextSelector({'special':['ABC','范昭平'],'general':['ａｂｃ','abc','沥青混凝土']})
        self.assertEqual(selected.terms,['ABC','范昭平','沥青混凝土'])
        terms=['范昭平','范招平','沥青混凝土','ABC','桥梁']
        first=ContextSelector({'general':terms});second=ContextSelector({'general':list(reversed(terms))})
        self.assertEqual(first.select('范招平介绍ABC桥梁沥青混凝土'),second.select('范招平介绍ABC桥梁沥青混凝土'))

    def test_text_homophone_and_true_syllable_edits(self):
        selector=ContextSelector({'general':['沥青混凝土']})
        for text in ('沥青、混凝土','立青混凝图','立青混凝路','立青混凝的土','立青混土'):
            self.assertEqual(selector.select(text),['沥青混凝土'])
        self.assertEqual(selector.select('立青混的路'),[])
        self.assertEqual(ContextSelector({'general':['ABCD','12345','范昭平']}).select('ABXD 12346 范招兵'),[])
        self.assertEqual(ContextSelector({'general':['范']}).select('饭'),[])

    def test_scores_length_and_stable_digest_ties(self):
        terms=['范昭平','范招平','沥青混凝土','桥梁工程','路基工程']
        selector=ContextSelector({'general':terms})
        result=selector.select('范招平，立青混凝路，桥梁工程和路基工程')
        self.assertEqual(result[:2],sorted(['桥梁工程','路基工程'],key=lambda t:hashlib.sha256(t.encode()).hexdigest()))
        self.assertEqual(result[2:],['范招平','范昭平','沥青混凝土'])

    def test_hard_caps_no_padding_and_skip_whole_oversized_term(self):
        terms=['identifier'+str(i).zfill(3) for i in range(100)]
        result=ContextSelector({'general':terms}).select(' '.join(terms),max_terms=1000,max_chars=10000)
        self.assertEqual(len(result),70);self.assertLessEqual(sum(len(t)+1 for t in result),1100)
        long='A'*1100;short='B'*12
        selector=ContextSelector({'general':[long,short]})
        self.assertEqual(selector.select(long+' '+short,max_chars=5000),[short])
        self.assertEqual(selector.select(short,max_terms=0),[])
        self.assertEqual(selector.select(short,max_chars=12),[])
        self.assertEqual(selector.select(short,max_chars=13),[short])

    def test_near_index_matches_brute_force_edit_distance(self):
        def distance(a,b):
            previous=list(range(len(b)+1))
            for i,x in enumerate(a,1):
                row=[i]
                for j,y in enumerate(b,1):row.append(min(row[-1]+1,previous[j]+1,previous[j-1]+(x!=y)))
                previous=row
            return previous[-1]
        rng=random.Random(254)
        for _ in range(800):
            n=rng.randint(4,10);term=tuple(rng.choice(('a','b','c','d')) for _ in range(n))
            words=[rng.choice(('a','b','c','d')) for _ in range(rng.randint(0,20))]
            expected=any(distance(term,words[i:i+size])<=1 for size in (n-1,n,n+1) for i in range(len(words)-size+1))
            self.assertEqual(_NearSounds(words,{n}).contains(term),expected)


@unittest.skipUnless(importlib.util.find_spec('soundfile') and importlib.util.find_spec('soxr'),'Run in the actual ASR environment')
class HotwordPipelineTests(unittest.TestCase):
    def setup_run(self,folder):
        import numpy as np
        import soundfile as sf
        from asr_worker import Engine
        audio=Path(folder)/'audio.wav';sf.write(audio,np.linspace(-.1,.1,6*16000,dtype='float32'),16000)
        engine=Engine.__new__(Engine)
        engine.device='cpu';engine.model_name='test';engine.model_signature='test'
        engine.torch=SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=type('OOM',(RuntimeError,),{})))
        engine.vad=None;engine.vad_function=None
        request=dict(path=str(audio),sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
            checkpoint=str(Path(folder)/'checkpoint.json'),vocabulary={'special':['范昭平'],'general':['沥青混凝土']},
            reference=str(Path(folder)/'nonexistent-old-reference.json'),asr_options={})
        chunks=[(0,4*16000),(4*16000,6*16000)]
        return engine,request,chunks

    def test_actual_first_prompts_empty_refine_only_related_same_audio(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as folder:
            engine,request,chunks=self.setup_run(folder);calls=[]
            def decode(items,selector,limit,retry=False):
                calls.append([(i['index'],selector.prompt(i['terms']),i['audio'].copy()) for i in items])
                return [dict(text=('补识别模型输出' if i['terms'] else ('范招平' if i['index']==0 else '今天天气晴朗')),failed=False) for i in items]
            engine._decode_batch=decode
            with patch('asr_worker.speech_chunks',return_value=chunks):result=engine.transcribe(request)
            self.assertEqual([len(c) for c in calls],[2,1])
            self.assertEqual([r[1] for r in calls[0]],['',''])
            self.assertEqual(calls[1][0][1],'相关词汇：范昭平')
            np.testing.assert_array_equal(calls[0][0][2],calls[1][0][2])
            self.assertEqual(result['segments'][0]['text'],'补识别模型输出')
            self.assertEqual(result['segments'][0]['first_pass_text'],'范招平')
            self.assertEqual(result['segments'][1]['context_terms'],[])
            self.assertEqual(result['context_strategy'],STRATEGY)
            self.assertEqual((result['context_max_terms'],result['context_max_chars']),(70,1100))
            self.assertEqual(result['context_refined_segments'],1);self.assertFalse(result['reference_used'])
            self.assertEqual(result['asr_options']['batch_size'],2)

    def test_pause_between_passes_recomputes_batch_and_archives_old_strategy(self):
        with tempfile.TemporaryDirectory() as folder:
            engine,request,chunks=self.setup_run(folder);calls=[];pause=[True]
            def decode(items,selector,limit,retry=False):
                calls.append([list(i['terms']) for i in items])
                if items[0]['terms'] and pause[0]:pause[0]=False;raise InterruptedError('pause before committing refinement')
                return [dict(text='范招平',failed=False) for i in items]
            engine._decode_batch=decode
            checkpoint=Path(request['checkpoint'])
            with patch('asr_worker.speech_chunks',return_value=chunks):
                with self.assertRaises(InterruptedError):engine.transcribe(request)
                self.assertEqual(json.loads(checkpoint.read_text())['completed_indices'],[])
                engine.transcribe(request)
                self.assertEqual(calls[2],[[],[]])
                state=json.loads(checkpoint.read_text());self.assertEqual(state['completed_indices'],[0,1])
                before=len(calls);engine.transcribe(request);self.assertEqual(len(calls),before)
                state['signature']=state['signature'].replace(STRATEGY,'old-context-rule')
                checkpoint.write_text(json.dumps(state),encoding='utf-8')
                engine.transcribe(request)
                self.assertEqual(len(list(Path(folder).glob('checkpoint.json.archived-*'))),1)
                self.assertEqual(calls[before],[[],[]])


@unittest.skipUnless(importlib.util.find_spec('torch'),'Run in the actual ASR environment')
class RecoveryPromptTests(unittest.TestCase):
    def test_short_retry_omits_hints_in_actual_processor_request_and_logs_it(self):
        import torch
        from asr_worker import Engine
        engine=Engine.__new__(Engine);engine.torch=torch
        class Inputs(dict):
            def to(self,*args):return self
        inputs=Inputs(input_ids=torch.tensor([[8]]),attention_mask=torch.tensor([[1]]))
        engine.processor=SimpleNamespace(apply_transcription_request=Mock(return_value=inputs),decode=lambda *a,**k:['恢复文本'])
        engine.model=SimpleNamespace(device='cpu',dtype=torch.float32,generation_config=SimpleNamespace(eos_token_id=9),
                                     generate=lambda **kwargs:torch.tensor([[8,9]]))
        item=dict(index=0,audio=[0]*16000,terms=['范昭平'],phase='refinement')
        with patch('asr_worker.emit') as log:
            engine._decode_batch([item],ContextSelector({}),4096,retry=True)
        self.assertEqual(engine.processor.apply_transcription_request.call_args.kwargs['prompt'],[''])
        self.assertEqual(engine.context_prompt_omissions[0]['segment_index'],0)
        self.assertTrue(log.call_args.kwargs['context_prompt_omitted'])


if __name__=='__main__':unittest.main()

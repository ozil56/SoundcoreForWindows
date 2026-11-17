import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from asr_worker import Engine, OutputLimit, adaptive_decode, checkpoint_segments, effective_options, archive_checkpoint


class BatchRecoveryTests(unittest.TestCase):
    def engine(self):
        engine=Engine.__new__(Engine)
        class OOM(RuntimeError):pass
        engine.torch=SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=OOM,empty_cache=Mock()))
        return engine,OOM

    def item(self,seconds=12,index=0):
        return dict(index=index,audio=np.zeros(16000*seconds,dtype=np.float32),terms=[],adaptive_retries=0)

    def test_office_limits_and_defaults(self):
        self.assertEqual(effective_options({}),dict(chunk_seconds=60,batch_size=2,max_new_tokens=4096))
        self.assertEqual(effective_options({'batch_size':4,'chunk_seconds':180})['batch_size'],2)
        self.assertEqual(effective_options({'chunk_seconds':180})['chunk_seconds'],60)

    def test_tail_single_oom_splits_without_losing_samples(self):
        engine,OOM=self.engine();seen=[];item=self.item()
        def decode(items,*args):
            size=len(items[0]['audio'])
            if size>3*16000:raise OOM()
            seen.append(size);return [dict(text='恢复文本',failed=False,reason='')]
        engine._decode_batch=decode
        engine._recognize_items([item],None,4096,0)
        self.assertEqual(sum(seen),len(item['audio']))
        self.assertTrue(item['text']);self.assertGreater(item['adaptive_retries'],0)
        self.assertTrue(all(e['reason']=='cuda_oom' for e in engine.retry_events))

    def test_batch_oom_retries_same_engine_one_row_at_a_time(self):
        engine,OOM=self.engine();calls=[];items=[self.item(index=i) for i in range(2)]
        def decode(batch,*args):
            calls.append(len(batch))
            if len(batch)>1:raise OOM()
            return [dict(text='单段恢复',failed=False,reason='')]
        engine._decode_batch=decode;engine._recognize_items(items,None,4096,0)
        self.assertEqual(calls,[2,1,1]);self.assertTrue(all(i['text']=='单段恢复' for i in items))

    def test_only_failed_row_retried_and_reason_preserved(self):
        for reason in ('repeating_tokens','output_limit','empty_output'):
            engine,_=self.engine();items=[self.item(index=i) for i in range(2)]
            engine._decode_batch=Mock(return_value=[dict(text='完整文本',failed=False),dict(failed=True,reason=reason)])
            engine._retry_segment=Mock(return_value=('恢复文本',1))
            engine._recognize_items(items,None,4096,0)
            self.assertEqual(items[0]['text'],'完整文本');self.assertEqual(items[1]['text'],'恢复文本')
            engine._retry_segment.assert_called_once_with(items[1],None,4096)
            self.assertEqual(engine.retry_events[0]['reason'],reason)

    def test_permanent_oom_has_bounded_failure(self):
        engine,OOM=self.engine();engine._decode_batch=Mock(side_effect=OOM())
        with self.assertRaises(OutputLimit):engine._recognize_items([self.item(1)],None,4096,0)
        self.assertEqual(engine._decode_batch.call_count,3)

    def test_sparse_progress_never_means_first_n_segments(self):
        state=dict(chunks=[[0,16000],[16000,32000],[32000,48000]],completed_indices=[0,2],
                   segments=[dict(start=0,end=1,text='甲'),dict(start=2,end=3,text='丙')])
        self.assertEqual(list(checkpoint_segments(state)),[0,2])
        state['completed_indices']=[0,0]
        with self.assertRaises(ValueError):checkpoint_segments(state)
        state['completed_indices']=[0,1]
        with self.assertRaises(ValueError):checkpoint_segments(state)
        state['completed_indices']=[0,2];state['segments'][1]['text']=' '
        with self.assertRaises(ValueError):checkpoint_segments(state)

    def test_archive_preserves_incompatible_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'checkpoint.json';path.write_text('old-progress')
            archive_checkpoint(path)
            self.assertFalse(path.exists());self.assertEqual(next(Path(folder).iterdir()).read_text(),'old-progress')


@unittest.skipUnless(importlib.util.find_spec('torch'), 'CUDA runtime suite runs separately')
class RowCompletionTests(unittest.TestCase):
    def test_eos_padding_does_not_hide_another_rows_truncation(self):
        import torch
        engine=Engine.__new__(Engine);engine.torch=torch
        class Inputs(dict):
            def to(self,*args):return self
        inputs=Inputs(input_ids=torch.tensor([[0,8],[7,8]]),attention_mask=torch.tensor([[0,1],[1,1]]))
        engine.processor=SimpleNamespace(apply_transcription_request=Mock(return_value=inputs),
                                        decode=Mock(return_value=['正常行','截断行']))
        def generate(**kwargs):
            guard=kwargs['stopping_criteria'][0]
            guard(torch.tensor([[0,8,9],[7,8,5]]),None)
            return torch.tensor([[0,8,9,0],[7,8,5,6]])
        engine.model=SimpleNamespace(device='cpu',dtype=torch.float32,
                                    generation_config=SimpleNamespace(eos_token_id=9),generate=generate)
        selector=SimpleNamespace(prompt=lambda _: '')
        items=[dict(audio=np.zeros(100),terms=[]) for _ in range(2)]
        answers=engine._decode_batch(items,selector,512)
        self.assertFalse(answers[0]['failed']);self.assertEqual(answers[1]['reason'],'output_limit')
        kwargs=engine.processor.apply_transcription_request.call_args.kwargs
        self.assertEqual(kwargs['processor_kwargs']['text_kwargs'],{'padding':True,'padding_side':'left'})

    def test_repeating_row_stops_independently(self):
        import torch
        engine=Engine.__new__(Engine);engine.torch=torch
        class Inputs(dict):
            def to(self,*args):return self
        inputs=Inputs(input_ids=torch.tensor([[8],[8]]),attention_mask=torch.ones((2,1),dtype=torch.int64))
        engine.processor=SimpleNamespace(apply_transcription_request=lambda **_:inputs,
                                        decode=lambda *a,**k:['正常文本','重复文本'])
        def generate(**kwargs):
            guard=kwargs['stopping_criteria'][0]
            first=torch.tensor([[8,9],[8,5]])
            guard(first,None)
            output=torch.tensor([[8,9]+[0]*47,[8]+[5]*48])
            self.assertEqual(guard(output,None).tolist(),[True,True])
            return output
        engine.model=SimpleNamespace(device='cpu',dtype=torch.float32,
                                    generation_config=SimpleNamespace(eos_token_id=9),generate=generate)
        result=engine._decode_batch([dict(audio=[],terms=[])]*2,SimpleNamespace(prompt=lambda _:''),512)
        self.assertFalse(result[0]['failed']);self.assertEqual(result[1]['reason'],'repeating_tokens')


@unittest.skipUnless(importlib.util.find_spec('soundfile') and importlib.util.find_spec('soxr'), 'Audio runtime suite runs separately')
class ResumeTests(unittest.TestCase):
    def test_pause_resume_sorted_batches_and_parameter_archive(self):
        import soundfile as sf
        with tempfile.TemporaryDirectory() as folder:
            audio=Path(folder)/'source.wav';checkpoint=Path(folder)/'checkpoint.json'
            sf.write(audio,np.zeros(12*16000),16000)
            engine=Engine.__new__(Engine)
            engine.device='cpu';engine.model_name='test';engine.model_signature='test'
            engine.torch=SimpleNamespace();engine.vad=None;engine.vad_function=None
            chunks=[(0,16000),(16000,5*16000),(5*16000,12*16000)]
            called=[]
            def recognize(items,*args):
                called.append([i['index'] for i in items])
                if len(called)==2:raise InterruptedError('pause within next batch')
                for item in items:item['text']='识别段'+str(item['index'])
            engine._recognize_items=recognize
            request=dict(path=str(audio),sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
                         checkpoint=str(checkpoint),vocabulary={},asr_options={})
            with patch('asr_worker.speech_chunks',return_value=chunks):
                with self.assertRaises(InterruptedError):engine.transcribe(request)
                state=json.loads(checkpoint.read_text(encoding='utf-8'))
                self.assertEqual(state['completed_indices'],[1,2])
                result=engine.transcribe(request)
                self.assertEqual(called,[[2,1],[0],[0]])
                self.assertEqual([s['text'] for s in result['segments']],['识别段0','识别段1','识别段2'])
                before=len(called);engine.transcribe(request);self.assertEqual(len(called),before)
                request['asr_options']={'batch_size':1}
                engine.transcribe(request)
                self.assertEqual(len(list(Path(folder).glob('checkpoint.json.archived-*'))),1)
                self.assertEqual(called[-3:],[[2],[1],[0]])


if __name__=='__main__':unittest.main()

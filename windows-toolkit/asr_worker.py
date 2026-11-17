"""Offline Qwen3-ASR worker. JSON lines over stdio; never uploads audio."""
import argparse
import hashlib
import io
from importlib import resources
import json
import os
from pathlib import Path
import sys
import time
import traceback
import uuid

# -I keeps bundled extensions, PYTHONPATH and user packages out of this process.
# Only these worker helpers are added, after the interpreter's own libraries.
sys.path.append(str(Path(__file__).resolve().parent))

from tool_settings import write_json
from vocabulary_context import ContextSelector, STRATEGY, MAX_TERMS, MAX_CHARS

ASR_STRATEGY = 'office-batch2-v6'


def effective_options(options):
    return {'chunk_seconds':max(15,min(60,int(options.get('chunk_seconds',60)))),
            'batch_size':max(1,min(2,int(options.get('batch_size',2)))),
            'max_new_tokens':max(512,min(8192,int(options.get('max_new_tokens',4096))))}


def archive_checkpoint(path):
    if path.exists():
        path.rename(path.with_name(path.name+'.archived-'+uuid.uuid4().hex))


def emit(event, **data):
    print(json.dumps(dict(event=event, **data), ensure_ascii=False), flush=True)


def speech_chunks(audio, vad_model, torch, get_speech_timestamps, max_seconds=60):
    timestamps = get_speech_timestamps(torch.from_numpy(audio), vad_model,
        sampling_rate=16000, min_speech_duration_ms=200,
        min_silence_duration_ms=500, max_speech_duration_s=max_seconds-1,
        speech_pad_ms=200, return_seconds=False)
    chunks = []
    for entry in timestamps:
        start, end = entry['start'], entry['end']
        if chunks and end-chunks[-1][0] <= max_seconds*16000:
            chunks[-1] = (chunks[-1][0], end)
        else:
            chunks.append((start, end))
    return chunks


class OutputLimit(Exception):
    pass


def repeating_tokens(tokens):
    """Conservative runaway detection; stopped text is retried, never accepted."""
    for size in range(1,25):
        repeats=max(6,(48+size-1)//size)
        count=size*repeats
        if len(tokens)>=count and tokens[-count:]==tokens[-size:]*repeats:
            return True
    return False


def adaptive_decode(start, end, decode, split_at=None):
    """Retry only a capped interval; never publish its partial token output."""
    try:
        return decode(start, end, False), 0
    except OutputLimit:
        if end-start <= 3*16000:
            # Very short looping outputs get a bounded repetition-controlled retry.
            return decode(start, end, True), 1
        middle = split_at(start,end) if split_at else (start+end)//2
        if not start < middle < end:
            raise ValueError('无法细分语音片段')
        left, a = adaptive_decode(start,middle,decode,split_at)
        right, b = adaptive_decode(middle,end,decode,split_at)
        return (left+' '+right).strip(), a+b+1


def checkpoint_segments(state):
    """Validate sparse progress; a batch may complete later timeline segments first."""
    indices=state.get('completed_indices',[]);segments=state.get('segments',[]);chunks=state['chunks']
    if (not isinstance(indices,list) or not isinstance(segments,list)
            or not all(type(i) is int for i in indices)
            or len(indices)!=len(segments) or indices!=sorted(set(indices))):
        raise ValueError('转写断点索引不一致')
    completed={}
    for index,segment in zip(indices,segments):
        if type(index) is not int or not 0<=index<len(chunks):raise ValueError('转写断点索引越界')
        start,end=chunks[index]
        if segment['start']!=start/16000 or segment['end']!=end/16000:raise ValueError('转写断点时间不一致')
        if not isinstance(segment.get('text'),str) or not segment['text'].strip():
            raise ValueError('转写断点包含空白语音段')
        completed[index]=segment
    return completed


class Engine:
    def __init__(self, model_dir):
        emit('progress', percent=0, message='正在加载本地模型（首次启动较慢）')
        import torch
        from transformers import AutoProcessor, AutoModelForMultimodalLM
        from silero_vad import get_speech_timestamps
        self.torch = torch
        torch.set_num_threads(4)
        self.device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
        dtype = torch.bfloat16 if self.device.startswith('cuda') else torch.float32
        self.processor = AutoProcessor.from_pretrained(model_dir, local_files_only=True)
        self.model = AutoModelForMultimodalLM.from_pretrained(model_dir,
            dtype=dtype, device_map=self.device, local_files_only=True,
            attn_implementation='sdpa').eval()
        # torch.jit's native path loader fails on some Windows Unicode paths.
        # Python opens the bundled resource safely; Torch loads the same bytes.
        vad_bytes = resources.files('silero_vad.data').joinpath('silero_vad.jit').read_bytes()
        self.vad = torch.jit.load(io.BytesIO(vad_bytes), map_location='cpu').eval()
        self.vad_function = get_speech_timestamps
        self.processor.tokenizer.padding_side = 'left'
        self.model_name = Path(model_dir).name
        self.model_signature = hashlib.sha256((Path(model_dir)/'config.json').read_bytes()).hexdigest()

    def transcribe(self, request):
        total_began=time.monotonic()
        self.retry_events=[]
        self.context_prompt_omissions=[]
        if self.device.startswith('cuda'):self.torch.cuda.reset_peak_memory_stats()
        import numpy as np
        import soundfile as sf
        path = Path(request['path'])
        with path.open('rb') as file:
            digest = hashlib.file_digest(file, 'sha256').hexdigest()
        if digest != request['sha256']:
            raise ValueError('音频校验与下载记录不一致，未进行转写')
        # Streaming decode/resample bounds temporary memory for multi-hour MP3.
        import soxr
        parts=[]
        with sf.SoundFile(path) as source:
            resampler=soxr.ResampleStream(source.samplerate,16000,1,dtype='float32') if source.samplerate!=16000 else None
            for block in source.blocks(blocksize=source.samplerate*30,dtype='float32',always_2d=True):
                mono=np.ascontiguousarray(block.mean(axis=1))
                parts.append(resampler.resample_chunk(mono,last=source.tell()==len(source)) if resampler else mono)
        audio=np.concatenate(parts) if parts else np.empty(0,dtype='float32')
        del parts
        checkpoint = Path(request['checkpoint'])
        snapshot=request.get('vocabulary',{})
        selector_began=time.monotonic()
        selector=ContextSelector(snapshot)
        selector_build_seconds=time.monotonic()-selector_began
        # Historical reference paths remain on old jobs, but never seed this strategy.
        # Do not read, change or delete the user's earlier reference/result files.
        reference_hash=''
        vocab_hash=hashlib.sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        options=effective_options(request.get('asr_options',{}))
        chunk_seconds=options['chunk_seconds'];token_limit=options['max_new_tokens'];batch_size=options['batch_size']
        signature = f'{digest}:{self.model_name}:{self.model_signature}:vad{chunk_seconds}-batch{batch_size}-t{token_limit}-{ASR_STRATEGY}:{STRATEGY}:{vocab_hash}'
        emit('progress',percent=0,message=f'识别参数：每批最多 {batch_size} 段，每段最长 {chunk_seconds} 秒',asr_options=options)
        state = {}
        try:
            state = json.loads(checkpoint.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        if not isinstance(state,dict):state={}
        vad_began=time.monotonic();vad_seconds=0
        if state.get('signature') != signature:
            archive_checkpoint(checkpoint)
            emit('progress', percent=0, message='正在分段并排除静音')
            chunks = speech_chunks(audio, self.vad, self.torch, self.vad_function,chunk_seconds)
            vad_seconds=time.monotonic()-vad_began
            state = {'signature':signature, 'chunks':chunks, 'segments':[], 'completed_indices':[],
                     'chunk_seconds':chunk_seconds,'batch_size':batch_size,'max_new_tokens':token_limit}
            write_json(checkpoint, state)
        chunks = state['chunks']
        completed = checkpoint_segments(state)
        self.context_prompt_omissions=list(state.get('context_prompt_omissions',[]))
        phase_seconds={'first_pass':0.0,'selection':0.0,'refinement':0.0}
        began = time.monotonic()
        # Group similarly long segments within this recording; persist their original indices.
        pending=sorted((i for i in range(len(chunks)) if i not in completed),
                       key=lambda i:chunks[i][1]-chunks[i][0],reverse=True)
        for offset in range(0,len(pending),batch_size):
            indices=pending[offset:offset+batch_size]
            percent=100*len(completed)/max(1,len(chunks))
            emit('progress', percent=percent,
                 message=f'本地转写：已完成 {len(completed)}/{len(chunks)} 段，本批 {len(indices)} 段（{self.device}）',
                 segment_boundary=True,batch_size=len(indices),segment_indices=indices)
            items=[]
            for index in indices:
                start,end=chunks[index]
                items.append({'index':index,'start':start,'end':end,'audio':audio[start:end],
                              'terms':[],'phase':'first_pass','adaptive_retries':0})
            phase_began=time.monotonic()
            self._recognize_items(items,selector,token_limit,percent)
            phase_seconds['first_pass']+=time.monotonic()-phase_began
            refined=[]
            phase_began=time.monotonic()
            for item in items:
                item['first_pass_text']=item['text']
                terms=selector.select(item['first_pass_text'])
                if terms:
                    item['terms']=terms;item['phase']='refinement';refined.append(item)
            phase_seconds['selection']+=time.monotonic()-phase_began
            if refined:
                emit('progress',percent=percent,message=f'词库补识别：本批 {len(refined)} 段')
                phase_began=time.monotonic()
                self._recognize_items(refined,selector,token_limit,percent)
                phase_seconds['refinement']+=time.monotonic()-phase_began
            for item in items:
                completed[item['index']]={'start':item['start']/16000,'end':item['end']/16000,
                    'text':item['text'],'first_pass_text':item['first_pass_text'],
                    'context_terms':item['terms'],'adaptive_retries':item['adaptive_retries']}
            state['completed_indices']=sorted(completed)
            state['segments']=[completed[i] for i in state['completed_indices']]
            state['context_prompt_omissions']=self.context_prompt_omissions
            write_json(checkpoint,state)
            emit('progress',percent=100*len(completed)/max(1,len(chunks)),
                 message=f'本地转写：已完成 {len(completed)}/{len(chunks)} 段（每批最多 {batch_size} 段）',
                 segment_boundary=True,batch_size=batch_size)
        segments=state['segments']
        if state['completed_indices']!=list(range(len(chunks))):raise ValueError('转写段落尚未完整完成')
        return dict(model=self.model_name, device=self.device, segments=segments,
                    audio_seconds=len(audio)/16000, elapsed_seconds=time.monotonic()-began,
                    worker_total_seconds=time.monotonic()-total_began,vad_seconds=vad_seconds,
                    preparation_seconds=began-total_began-vad_seconds,
                    selector_build_seconds=selector_build_seconds,phase_seconds=phase_seconds,
                    retry_events=self.retry_events,
                    context_prompt_omissions=self.context_prompt_omissions,
                    peak_cuda_allocated_bytes=self.torch.cuda.max_memory_allocated() if self.device.startswith('cuda') else 0,
                    peak_cuda_reserved_bytes=self.torch.cuda.max_memory_reserved() if self.device.startswith('cuda') else 0,
                    audio_sha256=digest, vocabulary_sha256=snapshot.get('sha256'),
                    asr_options=dict(options,retry_strategy=ASR_STRATEGY),
                    context_strategy=STRATEGY,context_max_terms=MAX_TERMS,context_max_chars=MAX_CHARS,
                    context_vocabulary_terms=len(selector.terms),
                    context_refined_segments=sum(bool(s['context_terms']) for s in segments),
                    reference_sha256=reference_hash,reference_used=False)

    def _decode_batch(self, items, selector, token_limit, retry=False):
        from transformers import StoppingCriteria,StoppingCriteriaList
        torch=self.torch
        if retry:
            for item in items:
                if item['terms']:
                    event={'segment_index':item.get('index'),'phase':item.get('phase'),
                           'audio_seconds':len(item['audio'])/16000,'reason':'short_segment_recovery'}
                    if not hasattr(self,'context_prompt_omissions'):self.context_prompt_omissions=[]
                    self.context_prompt_omissions.append(event)
                    emit('progress',message='异常短段重试：暂不使用热词提示',context_prompt_omitted=True,**event)
        inputs=self.processor.apply_transcription_request(
            audio=[item['audio'] for item in items],language=['Chinese']*len(items),
            prompt=['' if retry else selector.prompt(item['terms']) for item in items],
            processor_kwargs={'text_kwargs':{'padding':True,'padding_side':'left'}}
        ).to(self.model.device,self.model.dtype)
        prefix=inputs['input_ids'].shape[1]
        if inputs['input_ids'].shape[0]!=len(items):raise RuntimeError('处理器未生成完整批量输入')
        if len(items)>1 and not bool((inputs['attention_mask'][:,-1]==1).all()):
            raise RuntimeError('批量识别必须使用左侧 padding')
        eos=self.model.generation_config.eos_token_id
        eos=eos if isinstance(eos,list) else [eos]
        class LoopStop(StoppingCriteria):
            def __init__(guard):
                guard.hit=[False]*len(items);guard.done=[False]*len(items)
            def __call__(guard,input_ids,scores,**kwargs):
                size=input_ids.shape[1]-prefix
                for i in range(len(items)):
                    if guard.done[i] or guard.hit[i]:continue
                    if int(input_ids[i,-1]) in eos:guard.done[i]=True
                    elif size>=48 and size%16==0:
                        guard.hit[i]=repeating_tokens(input_ids[i,max(prefix,input_ids.shape[1]-160):].tolist())
                return torch.tensor([a or b for a,b in zip(guard.done,guard.hit)],device=input_ids.device,dtype=torch.bool)
        guard=LoopStop();limit=min(2048,token_limit) if retry else token_limit
        options={'repetition_penalty':1.08,'no_repeat_ngram_size':8} if retry else {}
        with torch.inference_mode():
            output=self.model.generate(**inputs,max_new_tokens=limit,do_sample=False,
                stopping_criteria=StoppingCriteriaList([guard]),**options)
        generated=output[:,prefix:]
        texts=self.processor.decode(generated,return_format='transcription_only')
        answers=[]
        for i,(tokens,text) in enumerate(zip(generated.tolist(),texts)):
            finished=any(token in eos for token in tokens)
            reason='repeating_tokens' if guard.hit[i] else ('output_limit' if not finished else ('empty_output' if not text.strip() else ''))
            answers.append({'text':text.strip(),'failed':bool(reason),'reason':reason})
        if len(answers)!=len(items):raise RuntimeError('语音模型返回的分段数量不符')
        return answers

    def _retry_segment(self,item,selector,token_limit):
        import numpy as np
        audio=item['audio']
        def split_at(a,b):
            middle=(a+b)//2
            candidates=range(max(a+16000,middle-4000),min(b-16000,middle+4000),160)
            return min(candidates,key=lambda x:float(np.mean(audio[x-160:x+160]**2)),default=middle)
        def decode(a,b,retry):
            try:
                answer=self._decode_batch([{**item,'audio':audio[a:b]}],selector,token_limit,retry)[0]
            except self.torch.cuda.OutOfMemoryError:
                self.torch.cuda.empty_cache()
                self._record_retry(item,'cuda_oom',b-a)
                raise OutputLimit('片段显存不足，需要细分') from None
            if answer['failed']:
                self._record_retry(item,answer['reason'],b-a)
                raise OutputLimit('片段重试仍未正常结束：'+answer['reason'])
            return answer['text']
        return adaptive_decode(0,len(audio),decode,split_at)

    def _recognize_items(self,items,selector,token_limit,percent):
        try:
            answers=self._decode_batch(items,selector,token_limit)
        except self.torch.cuda.OutOfMemoryError:
            # Release only this process's unused cache; retry rows in the same model instance.
            self.torch.cuda.empty_cache()
            answers=[{'failed':True,'reason':'cuda_oom'} for _ in items]
        for item,answer in zip(items,answers):
            if answer['failed']:
                self._record_retry(item,answer['reason'],len(item['audio']))
                emit('progress',percent=percent,
                     message=f'第 {item["index"]+1} 段需要单独重试，必要时自动拆小段',
                     retry_reason=answer['reason'],segment_index=item['index'])
                text,retries=self._retry_segment(item,selector,token_limit)
                item['text']=text;item['adaptive_retries']+=retries+1
            else:item['text']=answer['text']

    def _record_retry(self,item,reason,samples):
        if not hasattr(self,'retry_events'):self.retry_events=[]
        event={'segment_index':item['index'],'reason':reason,'audio_seconds':samples/16000,'phase':item.get('phase','unknown')}
        self.retry_events.append(event)
        emit('progress',message='异常段有界重试：'+reason,retry_reason=reason,**event)



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    engine = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if engine is None:
                engine = Engine(args.model)
            emit('result', **engine.transcribe(request))
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            emit('error', message=f'{type(error).__name__}: {error}')


if __name__ == '__main__':
    main()

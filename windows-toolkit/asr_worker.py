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

# -I keeps bundled extensions, PYTHONPATH and user packages out of this process.
# Only these worker helpers are added, after the interpreter's own libraries.
sys.path.append(str(Path(__file__).resolve().parent))

from tool_settings import write_json
from vocabulary_context import ContextSelector


def emit(event, **data):
    print(json.dumps(dict(event=event, **data), ensure_ascii=False), flush=True)


def speech_chunks(audio, vad_model, torch, get_speech_timestamps):
    timestamps = get_speech_timestamps(torch.from_numpy(audio), vad_model,
        sampling_rate=16000, min_speech_duration_ms=200,
        min_silence_duration_ms=500, max_speech_duration_s=24,
        speech_pad_ms=200, return_seconds=False)
    chunks = []
    for entry in timestamps:
        start, end = entry['start'], entry['end']
        if chunks and end-chunks[-1][0] <= 25*16000:
            chunks[-1] = (chunks[-1][0], end)
        else:
            chunks.append((start, end))
    return chunks


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
        self.model_name = Path(model_dir).name
        self.model_signature = hashlib.sha256((Path(model_dir)/'config.json').read_bytes()).hexdigest()

    def transcribe(self, request):
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
        selector=ContextSelector(snapshot)
        reference=[]; reference_hash=''
        if request.get('reference'):
            reference_bytes=Path(request['reference']).read_bytes()
            metadata=json.loads(reference_bytes.decode('utf-8-sig'))
            # References are only acoustic hint retrieval seeds, never output text.
            source_meta=metadata.get('source',{})
            if (isinstance(source_meta,dict) and source_meta.get('sha256')==digest) or metadata.get('audio_sha256')==digest:
                reference=metadata.get('segments',[])
                reference_hash=hashlib.sha256(reference_bytes).hexdigest()
        vocab_hash=hashlib.sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        signature = f'{digest}:{self.model_name}:{self.model_signature}:vad25-v2:{vocab_hash}:{reference_hash}'
        state = {}
        try:
            state = json.loads(checkpoint.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        if state.get('signature') != signature:
            emit('progress', percent=0, message='正在分段并排除静音')
            chunks = speech_chunks(audio, self.vad, self.torch, self.vad_function)
            state = {'signature':signature, 'chunks':chunks, 'segments':[]}
            write_json(checkpoint, state)
        chunks, segments = state['chunks'], state['segments']
        began = time.monotonic()
        for index in range(len(segments), len(chunks)):
            start, end = chunks[index]
            emit('progress', percent=100*index/max(1,len(chunks)),
                 message=f'本地转写第 {index+1}/{len(chunks)} 段（{self.device}）', segment_boundary=True)
            hint=' '.join(s['text'] for s in reference if s['end']>=start/16000-5 and s['start']<=end/16000+5)
            terms=selector.select(hint)
            def recognize(words):
                inputs = self.processor.apply_transcription_request(audio=audio[start:end], language='Chinese',
                    prompt=selector.prompt(words)).to(self.model.device, self.model.dtype)
                with self.torch.inference_mode():
                    output = self.model.generate(**inputs, max_new_tokens=512, do_sample=False)
                generated = output[:, inputs['input_ids'].shape[1]:]
                if generated.shape[1] >= 512:
                    raise ValueError('单段输出达到长度上限，未将截断文本标记为完成')
                return self.processor.decode(generated, return_format='transcription_only')[0].strip()
            text=recognize(terms)
            if not hint:
                refined=selector.select(text)
                if set(refined)-set(terms):
                    terms=refined; text=recognize(terms)
            segments.append({'start':start/16000, 'end':end/16000, 'text':text,'context_terms':terms})
            write_json(checkpoint, state)
        return dict(model=self.model_name, device=self.device, segments=segments,
                    audio_seconds=len(audio)/16000, elapsed_seconds=time.monotonic()-began,
                    audio_sha256=digest, vocabulary_sha256=snapshot.get('sha256'),
                    context_strategy='spelling-and-pinyin-retrieval-v1', reference_sha256=reference_hash)


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

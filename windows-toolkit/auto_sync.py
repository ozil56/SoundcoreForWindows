"""Idle BLE downloads and a durable local transcription queue.

The BLE worker owns the connection. The ASR worker never touches Bluetooth.
Completed receipts are the boundary between those two workers.
"""
import asyncio
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from recorder_files import (RecorderSession, RecordingStarted, download_folder,
                            download_one, load_selection, verify_download)
from tool_settings import read_settings, transcript_folder, write_json, in_schedule, schedule_label
from recording_catalog import CatalogMixin, is_external
from vocabulary import VocabularyStore,ProjectVocabularies
from tool_settings import diagnostics_folder, cache_folder
from asr_process import start_worker
from ollama_summary import enrich


class SyncDeferred(Exception):
    pass


class EnrichmentControl:
    """Pause between persisted parts when the schedule ends or ASR needs the GPU."""
    def __init__(self, pipeline, key):
        self.pipeline, self.key = pipeline, key

    def is_set(self):
        pipeline = self.pipeline
        settings = read_settings(pipeline.root)
        if pipeline.stop_event.is_set() or not settings.get('ollama_enabled', False):
            return True
        allowed = settings.get('auto_transcribe', False) and in_schedule(settings)
        if not allowed and not pipeline.library.runs_immediately(self.key):
            return True
        return pipeline.library.next_job(transcript_folder(pipeline.root), allow_auto=allowed) is not None


def recording_date(receipt):
    if receipt.get('recorded_at'):
        return dt.datetime.fromisoformat(receipt['recorded_at']), receipt.get('date_source','user_recording_date')
    timestamp = int(receipt['file_id'])
    if 946684800 <= timestamp <= 4102444800:
        return dt.datetime.fromtimestamp(timestamp).astimezone(), 'device_start_time'
    # An unknown device clock must not silently be labelled as recording time.
    timestamp = Path(receipt['path']).stat().st_mtime
    return dt.datetime.fromtimestamp(timestamp).astimezone(), 'audio_file_modified_time'


def dated_target(root, receipt):
    date, _ = recording_date(receipt)
    return (Path(root)/date.strftime('%Y')/date.strftime('%m')/date.strftime('%d')/
            f"{date:%H-%M-%S}_{receipt['identity_hex']}_{receipt['file_id']}.txt")


class Library(CatalogMixin):
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root/'sync-state.json'
        self.lock = threading.RLock()
        self.checked = {}
        try:
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            self.data = {'version':1, 'jobs':{}}
        self.data.setdefault('jobs', {})
        for job in self.data['jobs'].values():
            if job['status'] == 'running':
                job['status'] = 'pending'
        self.migrate_catalog()

    def save(self):
        write_json(self.path, self.data)

    def register(self, receipt):
        verify_download(receipt)
        key = f"{receipt['identity_hex']}:{receipt['file_id']}:{receipt['sha256']}"
        with self.lock:
            job = self.data['jobs'].get(key)
            if is_external(receipt):
                # Folder scans must never turn an external import into a daily job.
                record = self._ensure_record(key,receipt)
                record['receipt'] = receipt.copy()
                if job is not None: job['receipt'] = receipt.copy()
                self.save()
                return key
            if job is None:
                job = {'receipt':receipt, 'status':'pending', 'attempts':0, 'retry_at':0}
                self.data['jobs'][key] = job
                self._ensure_record(key,receipt)
                self.save()
            elif job['receipt']['path'] != receipt['path']:
                job['receipt'] = receipt
                self._ensure_record(key,receipt)['receipt'] = receipt
                self.save()
        return key

    def import_folder(self, folder):
        errors = []
        for path in Path(folder).glob('*.*.json'):
            if path.suffixes[-2].lower() not in ('.wav','.mp3','.flac','.ogg','.m4a'): continue
            try:
                receipt = json.loads(path.read_text(encoding='utf-8-sig'))
                audio = path.with_suffix('')
                # Receipts remain usable when the complete folder is moved.
                receipt['path'] = str(audio.resolve())
                stat = audio.stat()
                fingerprint = (path.stat().st_mtime_ns, stat.st_size, stat.st_mtime_ns)
                if self.checked.get(str(path)) == fingerprint:
                    continue
                self.register(receipt)
                self.checked[str(path)] = fingerprint
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append(f'{path.name}: {error}')
        return errors

    def verified_receipt(self, identity, recording):
        with self.lock:
            receipts = [j['receipt'].copy() for j in self.data['jobs'].values()
                        if j['receipt']['identity_hex'] == identity and j['receipt']['file_id'] == recording.file_id]
        for receipt in receipts:
            # v2 receipts predate list_size; that firmware reports 20 units/frame.
            if receipt.get('list_size', receipt.get('frames', 0)*20) != recording.size:
                continue
            try:
                verify_download(receipt)
                return receipt
            except (OSError, ValueError, KeyError):
                continue
        return None

    def has_download(self, identity, recording):
        return self.verified_receipt(identity, recording) is not None

    def next_job(self, output_root, allow_auto=True):
        with self.lock:
            for key, job in sorted(self.data['jobs'].items(),key=lambda item:(
                    not item[1].get('manual',False), not item[1].get('run_immediately',False))):
                if is_external(job['receipt']) and not job.get('manual'): continue
                if not allow_auto and not (job.get('manual') or job.get('run_immediately')): continue
                if job['status'] in ('running','cancelled'):
                    continue
                if job['status'] == 'complete':
                    continue  # A new destination must not rerun the entire history.
                if job.get('retry_at', 0) > time.time():
                    continue
                return key, json.loads(json.dumps(job))
        return None

    def request_device_transcription(self, identity):
        """Promote only unfinished daily jobs for the explicitly selected device."""
        with self.lock:
            changed = False
            for job in self.data['jobs'].values():
                if (job.get('manual') or is_external(job['receipt'])
                        or job['receipt']['identity_hex'] != identity
                        or job['status'] not in ('pending','failed','running')):
                    continue
                if not job.get('run_immediately') or job.get('retry_at',0):
                    job.update(run_immediately=True, retry_at=0)
                    changed = True
            if changed:
                self.save()

    def runs_immediately(self, key):
        with self.lock:
            job = self.data['jobs'][key]
            return bool(job.get('manual') or job.get('run_immediately'))

    def update(self, key, **changes):
        with self.lock:
            self.data['jobs'][key].update(changes)
            self.save()

    def counts(self):
        with self.lock:
            jobs = list(self.data['jobs'].values())
            return {state:sum(j['status'] == state for j in jobs)
                    for state in ('pending', 'running', 'complete', 'failed')}

    def retry_failed(self):
        with self.lock:
            for job in self.data['jobs'].values():
                if job['status']=='failed':
                    job['retry_at']=0
            self.save()


class AutoPipeline:
    def __init__(self, root, message):
        self.root, self.message = Path(root), message
        self.library = Library(root)
        self.vocabulary = VocabularyStore(root)
        self.projects = ProjectVocabularies(root)
        self.projects.migrate_text_files()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.worker = None
        self.process = None
        self.process_key = None
        self.output_lines = queue.Queue()
        self.sync_status = '等待设备空闲'
        self.asr_status = '等待本地转写'
        self.last_error = ''
        self.current_file = ''
        self.download_progress = 0
        self.transcribe_progress = 0
        self.next_sync = 0
        self.idle_since = None
        self.last_import = 0
        self.last_asr_use = 0
        self.receiver_id = None
        self.immediate_lock = threading.RLock()
        self.immediate_identity = None
        self.device_pending = None
        self.device_checked_at = None
        self.inventory_identity = None
        self.next_inventory = 0
        self.post_status = ''
        self.active_job = None

    def start(self):
        self.worker = threading.Thread(target=self._work, name='LocalTranscription', daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        process = self.process
        if process and process.poll() is None:
            process.terminate()

    def request_scan(self):
        self.next_sync = 0
        self.last_import = 0
        self.library.retry_failed()
        self.wake_event.set()

    def request_local_transcription(self):
        self.runtime()
        self.library.import_folder(download_folder(self.root))
        count=0
        with self.library.lock:
            for job in self.library.data['jobs'].values():
                if job['status'] in ('pending','failed','running'):
                    job.update(run_immediately=True,retry_at=0)
                    count+=1
            self.library.save()
        self.wake_event.set()
        self.message(f'立即转写全部剩余本地录音：{count} 个任务')
        return count

    def request_device_sync(self):
        selection = load_selection(self.root)
        if not selection:
            raise ValueError('请先选择录音豆，再点击“立即下载并转写设备录音”')
        self.runtime()  # Do not promise transcription when the model is missing.
        with self.immediate_lock:
            if self.immediate_identity == selection['identity_hex']:
                return False
            self.immediate_identity = selection['identity_hex']
            self.next_sync = 0
            self.last_import = 0
            self.sync_status = '已请求立即处理，等待设备连接并停止录音 15 秒'
        self.library.request_device_transcription(selection['identity_hex'])
        self.wake_event.set()
        self.message('立即下载并转写设备录音：本次不受每日时段和自动开关限制')
        return True

    async def when_idle(self, receiver):
        settings = read_settings(self.root)
        selection = load_selection(self.root)
        with self.immediate_lock:
            if self.immediate_identity and (not selection or selection['identity_hex'] != self.immediate_identity):
                self.immediate_identity = None
                self.message('已切换录音豆，取消上一设备尚未完成的立即下载请求')
                return
            immediate = bool(self.immediate_identity)
        if selection and getattr(receiver,'identity_hex',selection['identity_hex'])!=selection['identity_hex']:
            return
        # A read-only inventory lets the dashboard show device backlog outside
        # the transfer window. Never contend with physical recording.
        if (selection and not receiver.recording_active and receiver.file_id is None
                and time.monotonic() >= self.next_inventory and not immediate):
            self.next_inventory=time.monotonic()+120
            try:
                inventory=RecorderSession.attached(receiver,selection['identity_hex'])
                listed=await inventory.list_files()
                missing=0
                for item in listed:
                    if not item.active and not await asyncio.to_thread(self.library.has_download,selection['identity_hex'],item):
                        missing+=1
                self.device_pending=missing
                self.device_checked_at=dt.datetime.now().astimezone().isoformat()
                self.inventory_identity=selection['identity_hex']
            except Exception:
                pass
        if self.receiver_id != id(receiver):
            self.receiver_id = id(receiver)
            self.idle_since = None
        if not immediate and not in_schedule(settings):
            self.sync_status = '等待运行时间：'+schedule_label(settings)
            self.idle_since = None
            return
        if not immediate and not settings.get('auto_sync', False):
            self.sync_status = '自动下载已关闭'
            self.idle_since = None
            return
        if receiver.recording_active or receiver.file_id is not None:
            self.idle_since = None
            self.sync_status = '录音中，后台下载等待'
            return
        now = time.monotonic()
        if self.idle_since is None:
            self.idle_since = now
        if now-self.idle_since < 15 or now < self.next_sync:
            return
        self.next_sync = now+60
        if not selection:
            return
        session = RecorderSession.attached(receiver, selection['identity_hex'])
        guard_checked = 0
        def transfer_guard():
            nonlocal guard_checked
            now = time.monotonic()
            if now-guard_checked < .5:
                return
            guard_checked = now
            current = read_settings(self.root)
            if not immediate and (not current.get('auto_sync', False) or not in_schedule(current)):
                raise SyncDeferred('已到结束时间或自动下载已关闭')
        session.transfer_guard = transfer_guard
        try:
            self.sync_status = '正在检查新录音'
            files = await session.list_files()
            self.device_pending=0
            for item in files:
                if not item.active and not await asyncio.to_thread(self.library.has_download,selection['identity_hex'],item):
                    self.device_pending+=1
            self.device_checked_at=dt.datetime.now().astimezone().isoformat()
            self.inventory_identity=selection['identity_hex']
            if receiver.recording_active or any(f.active for f in files):
                self.idle_since = None
                self.sync_status = '录音中，后台下载等待'
                return
            chosen = None
            for recording in sorted(files, key=lambda f:f.file_id):
                receipt = await asyncio.to_thread(self.library.verified_receipt, selection['identity_hex'], recording)
                if receipt is None:
                    chosen = recording
                    break
                if read_settings(self.root).get('auto_delete', False):
                    await self.delete_saved(session, receipt)
            if chosen is None:
                if immediate:
                    self.library.request_device_transcription(selection['identity_hex'])
                    with self.immediate_lock:
                        self.immediate_identity = None
                    self.wake_event.set()
                    self.sync_status = '本次设备录音已同步，未完成转写继续处理'
                    self.message(self.sync_status)
                else:
                    self.sync_status = '设备录音已同步，等待新录音'
                return
            self.current_file = chosen.title
            self.sync_status = f'{"立即下载" if immediate else "自动下载"}：{chosen.title}'
            self.download_progress = 0
            receiver.phase = 'syncing'
            def progress(done, total):
                self.download_progress = min(99, 100*done/max(1,total))
            result = await download_one(session, chosen, download_folder(self.root), False, progress)
            await asyncio.to_thread(self.library.register, result)
            self.device_pending=max(0,(self.device_pending or 1)-1)
            if immediate:
                self.library.request_device_transcription(selection['identity_hex'])
                self.wake_event.set()
            if read_settings(self.root).get('auto_delete', False):
                await self.delete_saved(session, result)
            self.download_progress = 100
            self.last_error = ''
            self.sync_status = '下载完成，已加入本地转写队列'
            self.message(f'{"立即下载" if immediate else "自动下载"}完成：{Path(result["path"]).name}')
            self.next_sync = 0  # next completed file, yielding to realtime between files
            self.wake_event.set()
        except RecordingStarted:
            self.sync_status = '设备开始录音，已让出后台下载'
            self.idle_since = None
            self.next_sync = 0
        except SyncDeferred:
            self.sync_status = '已暂停下载，等待下一运行时间段'
            self.idle_since = None
        except asyncio.CancelledError:
            self.sync_status = '后台下载已暂停，下次空闲重试'
            self.idle_since = None
            raise
        except Exception as error:
            self.last_error = f'{type(error).__name__}: {error}'
            self.sync_status = '下载暂未完成，稍后自动重试'
            self.message(f'后台同步：{self.last_error}')
        finally:
            if receiver.phase == 'syncing':
                receiver.phase = 'waiting'

    async def delete_saved(self, session, receipt):
        # Recheck the actual bytes immediately before deleting, even for a prior
        # download whose deletion failed or was interrupted on an earlier run.
        await asyncio.to_thread(verify_download, receipt)
        if not read_settings(self.root).get('auto_delete', False):
            return
        session.check_idle()
        await session.delete(receipt['file_id'])
        receipt['deleted_from_device'] = True
        write_json(Path(receipt['path']).with_suffix('.wav.json'), receipt)
        self.message(f'已校验本地音频并删除设备原件：{receipt["file_id"]}')

    def runtime(self):
        settings = read_settings(self.root)
        python = Path(settings.get('asr_python') or self.root/'.asr-venv'/'Scripts'/'python.exe')
        model = Path(settings.get('asr_model_dir') or self.root/'models'/'Qwen3-ASR-1.7B-hf')
        if not python.is_file() or not (model/'config.json').is_file() or not list(model.glob('*.safetensors')):
            raise RuntimeError('尚未配置本地模型，请打开“设置”配置运行程序和模型目录')
        return python, model

    def _close_process(self):
        process, self.process = self.process, None
        if process:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout):
                if pipe:
                    pipe.close()

    def _start_process(self):
        python, model = self.runtime()
        key = (str(python), str(model))
        if self.process and self.process.poll() is None and key == self.process_key:
            return
        self._close_process()
        log_path = diagnostics_folder(self.root)/'local-asr.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('a', encoding='utf-8') as log:
            self.process = start_worker(python, model, log)
        self.process_key = key
        self.output_lines = queue.Queue()
        lines, pipe = self.output_lines, self.process.stdout
        def read():
            try:
                for line in pipe:
                    try:
                        lines.put(json.loads(line))
                    except ValueError:
                        continue
            finally:
                lines.put({'event':'closed'})
        threading.Thread(target=read, name='ASROutput', daemon=True).start()

    def transcribe(self, key, job, output_root):
        receipt = job['receipt']
        verify_download(receipt)
        if 'vocabulary' not in job:
            job['vocabulary']=self.vocabulary.snapshot()
            with self.library.lock:
                record=self.library.data['records'][job.get('record_id',key)]
                job.update(project=record['project'],title=record['title'])
            self.library.update(key,vocabulary=job['vocabulary'],project=job['project'],title=job['title'])
        began=time.monotonic()
        self._start_process()
        checkpoint = cache_folder(self.root)/(hashlib.sha256(key.encode()).hexdigest()+'.json')
        request = {'path':receipt['path'], 'sha256':receipt['sha256'], 'checkpoint':str(checkpoint),
                   'vocabulary':job['vocabulary'], 'reference':job.get('reference'),
                   'asr_options':{'chunk_seconds':read_settings(self.root).get('asr_chunk_seconds',60),
                                  'max_new_tokens':read_settings(self.root).get('asr_max_new_tokens',4096)}}
        self.process.stdin.write(json.dumps(request, ensure_ascii=False)+'\n')
        self.process.stdin.flush()
        deadline = time.monotonic()+900
        while not self.stop_event.is_set():
            current = read_settings(self.root)
            with self.library.lock:
                cancelled=self.library.data['jobs'][key]['status']=='cancelled'
            if cancelled or (not self.library.runs_immediately(key) and (not current.get('auto_transcribe', False) or not in_schedule(current))):
                self._close_process()
                raise InterruptedError('已到结束时间或暂停转写，已完成分段会保留')
            try:
                event = self.output_lines.get(timeout=.5)
            except queue.Empty:
                if time.monotonic() > deadline:
                    raise TimeoutError('本地模型长时间没有返回进度')
                continue
            if self.stop_event.is_set():
                raise InterruptedError('程序退出，转写进度已保存')
            if event['event'] == 'progress':
                if event.get('segment_boundary') and not job.get('manual') and self.library.has_manual():
                    self._close_process()
                    raise InterruptedError('优先处理手动转写，自动任务稍后续写')
                self.transcribe_progress = event.get('percent', 0)
                self.asr_status = event.get('message', '正在转写')
                deadline = time.monotonic()+900
            elif event['event'] == 'result':
                verify_download(receipt)
                event.update(vocabulary=job['vocabulary'], project=job.get('project',''),
                    title=job.get('title',''), version_id=job.get('version_id'), manual=bool(job.get('manual')), kind=job.get('kind'),
                    total_elapsed_seconds=time.monotonic()-began,
                    finished_at=dt.datetime.now().astimezone().isoformat(),job_created_at=job.get('created_at'))
                output = self.publish_text(receipt, event, output_root)
                self.library.finish_version(key,output,job,output_root)
                if read_settings(self.root).get('ollama_enabled',False):
                    self.library.update(key,enrichment_state='pending',enrichment_retry_at=0)
                self.asr_status = f'转写完成：{output.name}'
                self.transcribe_progress = 100
                self.last_error = ''
                self.message(self.asr_status)
                self.last_asr_use = time.monotonic()
                return
            elif event['event'] in ('error', 'closed'):
                raise RuntimeError(event.get('message') or '本地转写进程已退出，请检查日志')
        raise InterruptedError('程序退出，转写进度已保存')

    def publish_text(self, receipt, result, output_root):
        target = dated_target(output_root, receipt)
        settings=read_settings(self.root)
        if settings.get('ollama_enabled') and receipt.get('seconds',0)>=settings.get('ollama_min_seconds',120):
            target=target.parent/'原始转写'/target.name
        if result.get('version_id'):
            label='本地转写' if result.get('kind')=='local' else '专用词库'
            target=target.with_stem(target.stem+'_'+label+'_'+result['version_id'][:8])
        target.parent.mkdir(parents=True, exist_ok=True)
        date, date_source = recording_date(receipt)
        segments = result.get('segments', [])
        def clock(seconds):
            seconds=int(seconds); return f'{seconds//3600:02}:{seconds%3600//60:02}:{seconds%60:02}'
        body = '\n\n'.join(f"[{clock(s.get('start',0))}–{clock(s.get('end',0))}]\n{s['text'].strip()}"
                           for s in segments if s['text'].strip())
        if not body:
            body = '（未检测到可识别的语音）'
        date_labels={'device_start_time':'设备录音开始时间','audio_file_modified_time':'音频文件修改时间（设备时钟无效）',
                     'external_file_modified_time':'外部音频文件修改时间','user_recording_date':'指定录音日期'}
        content = (f'录音名称：{result.get("title") or Path(receipt["path"]).stem}\n'
                   f'项目：{result.get("project") or "未归类"}\n'
                   f'录音时间：{date:%Y-%m-%d %H:%M:%S %z}\n'
                   f'时间依据：{date_labels.get(date_source,date_source)}\n'
                   f'录音时长：{result.get("audio_seconds",receipt["seconds"]):.2f} 秒\n'
                   f'本地模型：{result.get("model", "Qwen3-ASR-1.7B")}\n'
                   f'音频文件：{receipt["path"]}\n\n{body}\n')
        # Preserve pre-existing and user-edited text, including crash recovery.
        for index in range(10000):
            candidate = target if not index else target.with_stem(target.stem+f'_{index}')
            if candidate.exists():
                if candidate.read_text(encoding='utf-8-sig') == content:
                    return candidate
                continue
            temporary = candidate.with_suffix('.txt.part')
            with temporary.open('w', encoding='utf-8-sig') as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            try:
                temporary.rename(candidate)
            except FileExistsError:
                temporary.unlink(missing_ok=True)
                continue
            write_json(candidate.with_suffix('.txt.json'), dict(result, source=receipt,
                recorded_at=date.isoformat(), date_source=date_source))
            return candidate
        raise RuntimeError('同名文本过多，未覆盖已有文件')

    def _work(self):
        try:
            while not self.stop_event.is_set():
                settings = read_settings(self.root)
                try:
                    if time.monotonic()-self.last_import > 15:
                        errors = self.library.import_folder(download_folder(self.root))
                        self.last_import = time.monotonic()
                        if errors:
                            self.last_error = errors[0]
                        with self.immediate_lock:
                            if self.immediate_identity:
                                self.library.request_device_transcription(self.immediate_identity)
                    allowed = settings.get('auto_transcribe', False) and in_schedule(settings)
                    task = self.library.next_job(transcript_folder(self.root),allow_auto=allowed)
                    if task:
                        key, job = task
                        self.active_job=key
                        self.runtime()  # missing setup does not poison every queue item
                        self.library.update(key, status='running', attempts=job['attempts']+1)
                        self.asr_status = f'准备转写：{Path(job["receipt"]["path"]).name}'
                        self.transcribe_progress = 0
                        try:
                            self.transcribe(key, job, transcript_folder(self.root))
                        except InterruptedError:
                            with self.library.lock:
                                if self.library.data['jobs'][key]['status']!='cancelled':
                                    self.library.update(key, status='pending', retry_at=0)
                        except Exception as error:
                            if self.stop_event.is_set():
                                self.library.update(key,status='pending',retry_at=0)
                                break
                            self.library.update(key, status='failed', error=str(error), retry_at=time.time()+300)
                            self._close_process()
                            raise
                        continue
                    self.active_job=None
                    if settings.get('ollama_enabled',False):
                        with self.library.lock:
                            post=next(((k,json.loads(json.dumps(j))) for k,j in self.library.data['jobs'].items()
                                if j['status']=='complete' and j.get('enrichment_state') in ('pending','running','failed')
                                and j.get('enrichment_retry_at',0)<=time.time()
                                and (allowed or j.get('manual') or j.get('run_immediately'))),None)
                        if post:
                            key,job=post
                            self.active_job=key
                            self._close_process()  # ASR and Ollama take turns using GPU memory.
                            self.library.update(key,enrichment_state='running')
                            try:
                                def progress(label,percent):
                                    self.post_status=label;self.transcribe_progress=percent
                                result=enrich(job['output'],settings,progress,EnrichmentControl(self,key))
                                self.library.update(key,enrichment_state=result.pop('state'),**result,enrichment_error='')
                            except InterruptedError:
                                self.library.update(key,enrichment_state='pending')
                            except Exception as error:
                                self.library.update(key,enrichment_state='failed',enrichment_error=str(error),enrichment_retry_at=time.time()+300)
                                self.message('转写已保存；纪要稍后重试：'+str(error))
                            finally:
                                self.post_status='';self.active_job=None
                            continue
                    if self.process and time.monotonic()-self.last_asr_use > 60:
                        self._close_process()  # release GPU when the queue is empty
                    if not settings.get('auto_transcribe', False):
                        self.asr_status = '手动转写队列空闲；日常录音自动转写已关闭'
                    elif not in_schedule(settings):
                        self.asr_status = '手动转写队列空闲；日常录音等待：'+schedule_label(settings)
                except Exception as error:
                    self.last_error = f'{type(error).__name__}: {error}'
                    self.asr_status = self.last_error
                self.wake_event.wait(5)
                self.wake_event.clear()
        finally:
            self._close_process()

    def snapshot(self):
        settings = read_settings(self.root)
        return {'auto_sync':bool(settings.get('auto_sync')), 'auto_transcribe':bool(settings.get('auto_transcribe')),
            'auto_delete':bool(settings.get('auto_delete')), 'schedule':schedule_label(settings),
            'sync_status':self.sync_status, 'asr_status':self.asr_status, 'last_error':self.last_error,
            'download_progress':self.download_progress, 'transcribe_progress':self.transcribe_progress,
            'immediate_download_pending':bool(self.immediate_identity),
            'device_pending':self.device_pending,'device_checked_at':self.device_checked_at,
            'post_status':self.post_status,'active_job':self.active_job,
            'counts':self.library.counts(), 'transcript_dir':str(transcript_folder(self.root)),
            'worker_running':bool(self.worker and self.worker.is_alive())}

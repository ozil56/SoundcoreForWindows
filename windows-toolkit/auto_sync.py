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
import sys
import threading
import time

from recorder_files import (RecorderSession, RecordingStarted, download_folder,
                            download_one, load_selection, verify_download)
from tool_settings import read_settings, transcript_folder, write_json, in_schedule, schedule_label
from recording_catalog import CatalogMixin
from vocabulary import VocabularyStore
from tool_settings import diagnostics_folder, cache_folder


class SyncDeferred(Exception):
    pass


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
            for key, job in sorted(self.data['jobs'].items(),key=lambda item:not item[1].get('manual',False)):
                if not allow_auto and not job.get('manual'): continue
                if job['status'] in ('running','cancelled'):
                    continue
                if job['status'] == 'complete':
                    if job.get('manual'): continue
                    previous_root = job.get('output_root')
                    if previous_root == str(Path(output_root).resolve()):
                        continue
                    job['status'] = 'pending'
                if job.get('retry_at', 0) > time.time():
                    continue
                return key, json.loads(json.dumps(job))
        return None

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

    async def when_idle(self, receiver):
        settings = read_settings(self.root)
        if self.receiver_id != id(receiver):
            self.receiver_id = id(receiver)
            self.idle_since = None
        if not in_schedule(settings):
            self.sync_status = '等待运行时间：'+schedule_label(settings)
            self.idle_since = None
            return
        if not settings.get('auto_sync', False):
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
        selection = load_selection(self.root)
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
            if not current.get('auto_sync', False) or not in_schedule(current):
                raise SyncDeferred('已到结束时间或自动下载已关闭')
        session.transfer_guard = transfer_guard
        try:
            self.sync_status = '正在检查新录音'
            files = await session.list_files()
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
                self.sync_status = '设备录音已同步，等待新录音'
                return
            self.current_file = chosen.title
            self.sync_status = f'自动下载：{chosen.title}'
            self.download_progress = 0
            receiver.phase = 'syncing'
            def progress(done, total):
                self.download_progress = min(99, 100*done/max(1,total))
            result = await download_one(session, chosen, download_folder(self.root), False, progress)
            await asyncio.to_thread(self.library.register, result)
            if read_settings(self.root).get('auto_delete', False):
                await self.delete_saved(session, result)
            self.download_progress = 100
            self.last_error = ''
            self.sync_status = '下载完成，已加入本地转写队列'
            self.message(f'自动下载完成：{Path(result["path"]).name}')
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
            raise RuntimeError('尚未配置本地模型，请打开“自动同步与转写”设置')
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
        worker = Path(__file__).resolve().parent/'asr_worker.py'
        environment = os.environ.copy()
        environment.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', HF_HUB_OFFLINE='1',
                           TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
        log_path = diagnostics_folder(self.root)/'local-asr.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('a', encoding='utf-8') as log:
            self.process = subprocess.Popen([str(python), '-u', str(worker), '--model', str(model)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                encoding='utf-8', env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
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
                   'vocabulary':job['vocabulary'], 'reference':job.get('reference')}
        self.process.stdin.write(json.dumps(request, ensure_ascii=False)+'\n')
        self.process.stdin.flush()
        deadline = time.monotonic()+900
        while not self.stop_event.is_set():
            current = read_settings(self.root)
            with self.library.lock:
                cancelled=self.library.data['jobs'][key]['status']=='cancelled'
            if cancelled or (not job.get('manual') and (not current.get('auto_transcribe', False) or not in_schedule(current))):
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
                    title=job.get('title',''), version_id=job.get('version_id'), manual=bool(job.get('manual')),
                    total_elapsed_seconds=time.monotonic()-began,
                    finished_at=dt.datetime.now().astimezone().isoformat(),job_created_at=job.get('created_at'))
                output = self.publish_text(receipt, event, output_root)
                self.library.finish_version(key,output,job,output_root)
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
        if result.get('version_id'):
            target=target.with_stem(target.stem+'_专用词库_'+result['version_id'][:8])
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
                    allowed = settings.get('auto_transcribe', False) and in_schedule(settings)
                    task = self.library.next_job(transcript_folder(self.root),allow_auto=allowed)
                    if task:
                        key, job = task
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
                    if self.process and time.monotonic()-self.last_asr_use > 60:
                        self._close_process()  # release GPU when the queue is empty
                    if not settings.get('auto_transcribe', False):
                        self.asr_status = '自动转写已关闭，进度已保存'
                    elif not in_schedule(settings):
                        self.asr_status = '等待运行时间：'+schedule_label(settings)
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
            'counts':self.library.counts(), 'transcript_dir':str(transcript_folder(self.root)),
            'worker_running':bool(self.worker and self.worker.is_alive())}

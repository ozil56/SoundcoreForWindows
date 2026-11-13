"""Explicit external audio submissions, independent of daily device automation."""
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from tool_settings import imported_audio_folder, transcript_folder
from vocabulary import parse_terms

AUDIO_TYPES = [('音频（WAV / MP3 / FLAC / OGG）', '*.wav *.mp3 *.flac *.ogg')]
SUPPORTED_SUFFIXES = {'.wav', '.mp3', '.flac', '.ogg'}


def submit_local_batch(pipeline, sources, folder, snapshot, project, report):
    """Copy/verify before submitting. Each file succeeds or fails independently."""
    successful = []
    errors = []
    for index, source in enumerate(sources, 1):
        source = Path(source)
        if pipeline.stop_event.is_set():
            break
        report(f'正在保存并校验 {index}/{len(sources)}：{source.name}')
        try:
            if source.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise ValueError('支持 WAV、MP3、FLAC、OGG 音频')
            record_id = pipeline.library.import_audio(source, folder)
            if pipeline.stop_event.is_set():
                break
            # Keep project selection and job creation together against other panels.
            with pipeline.library.lock:
                pipeline.library.edit_record([record_id], project=project)
                pipeline.library.queue_manual(record_id, snapshot, kind='local', reuse_pending=True)
            pipeline.wake_event.set()
            successful.append(str(source))
        except Exception as error:
            errors.append(f'{source.name}：{error}')
    return successful, errors


class LocalTranscriptionPanel:
    def __init__(self, app, root):
        self.app, self.root = app, Path(root)
        self.closed = False
        self.busy = False
        self.paths = []
        self.events = queue.Queue()
        self.worker = None
        self.window = tk.Toplevel(app.root)
        self.window.title('转写本地音频')
        self.window.geometry('880x780')
        self.window.minsize(780, 700)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        outer = ttk.Frame(self.window, padding=18)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='转写本地音频', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='用于其他录音设备或电脑上的音频；无需连接录音豆。点击开始后立即排队，不受每日自动时段限制。',
                  wraplength=800).pack(anchor='w', pady=(6, 10))

        bar = ttk.Frame(outer); bar.pack(fill='x')
        self.add_button = ttk.Button(bar, text='添加音频…（可多选）', command=self.choose_files)
        self.add_button.pack(side='left')
        self.remove_button = ttk.Button(bar, text='移除所选', command=self.remove_selected)
        self.remove_button.pack(side='left', padx=8)
        self.file_count = tk.StringVar(value='尚未选择音频')
        ttk.Label(bar, textvariable=self.file_count).pack(side='left')
        files = ttk.Frame(outer); files.pack(fill='x', pady=8)
        self.file_list = tk.Listbox(files, height=5, selectmode='extended', exportselection=False)
        scroll = ttk.Scrollbar(files, command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.file_list.pack(fill='x', expand=True)
        self.file_list.bind('<<ListboxSelect>>', self.show_path)
        self.path_text = tk.StringVar(value='只选择文件不会开始转写；原始音频始终保留。')
        ttk.Label(outer, textvariable=self.path_text, wraplength=800).pack(anchor='w')

        row = ttk.Frame(outer); row.pack(fill='x', pady=(12, 8))
        ttk.Label(row, text='项目（可留空）').pack(side='left')
        self.project = tk.StringVar()
        with app.pipeline.library.lock:
            projects = list(app.pipeline.library.data['projects'])
        self.project_box = ttk.Combobox(row, textvariable=self.project, values=projects, width=42)
        self.project_box.pack(side='left', padx=8)
        ttk.Label(row, text='本批音频共用此项目和词库').pack(side='left')
        row = ttk.Frame(outer); row.pack(fill='x')
        self.general = tk.StringVar()
        ttk.Label(row, textvariable=self.general).pack(side='left')
        ttk.Button(row, text='管理通用词库', command=app.show_vocabulary).pack(side='right')
        terms_frame = ttk.LabelFrame(outer, text='本次专用词库（可留空）', padding=10)
        terms_frame.pack(fill='both', expand=True, pady=8)
        row = ttk.Frame(terms_frame); row.pack(fill='x')
        ttk.Label(row, text='每行一个词或完整短语，词内空格保留；可直接粘贴或导入 UTF-8 TXT。').pack(side='left')
        self.import_button = ttk.Button(row, text='导入词库 TXT…', command=self.import_terms)
        self.import_button.pack(side='right')
        editor = ttk.Frame(terms_frame); editor.pack(fill='both', expand=True, pady=8)
        self.terms = tk.Text(editor, height=8, wrap='word', font=('Microsoft YaHei UI', 10), undo=True)
        scroll = ttk.Scrollbar(editor, command=self.terms.yview)
        self.terms.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.terms.pack(fill='both', expand=True)
        self.term_count = tk.StringVar()
        ttk.Label(terms_frame, textvariable=self.term_count).pack(anchor='w')

        self.storage = tk.StringVar(value=f'音频副本：{imported_audio_folder(root)}\n文本（年 / 月 / 日）：{transcript_folder(root)}')
        ttk.Label(outer, textvariable=self.storage, wraplength=800).pack(anchor='w', pady=(0, 8))
        actions = ttk.Frame(outer); actions.pack(fill='x')
        self.start_button = ttk.Button(actions, text='开始转写', command=self.start)
        self.start_button.pack(side='left')
        ttk.Button(actions, text='查看任务与结果', command=app.show_library).pack(side='left', padx=8)
        ttk.Button(actions, text='模型 / 文本目录设置', command=app.show_sync_panel).pack(side='right')
        self.status = tk.StringVar(value='就绪。提交后可关闭面板，程序会继续转写；重复转写会保留旧版本。')
        ttk.Label(outer, textvariable=self.status, wraplength=800).pack(anchor='w', pady=(8, 0))
        self.progress = tk.StringVar()
        ttk.Label(outer, textvariable=self.progress, wraplength=800, foreground='#666666').pack(anchor='w')
        self.poll()

    def add_files(self, paths):
        if self.busy: return
        for path in paths:
            value = str(Path(path).resolve())
            if value not in self.paths: self.paths.append(value)
        self.refresh_files()

    def choose_files(self):
        paths = filedialog.askopenfilenames(parent=self.window, title='选择要转写的本地音频', filetypes=AUDIO_TYPES)
        self.add_files(paths)

    def refresh_files(self):
        self.file_list.delete(0, 'end')
        for path in self.paths: self.file_list.insert('end', Path(path).name)
        self.file_count.set(f'已选择 {len(self.paths)} 个音频')

    def show_path(self, _event=None):
        selected = self.file_list.curselection()
        if selected: self.path_text.set(self.paths[selected[0]])

    def remove_selected(self):
        if self.busy: return
        for index in reversed(self.file_list.curselection()): del self.paths[index]
        self.refresh_files()

    def import_terms(self):
        path = filedialog.askopenfilename(parent=self.window, title='导入本次专用词库', filetypes=[('UTF-8 词库', '*.txt')])
        if not path: return
        try:
            terms = parse_terms(self.terms.get('1.0', 'end')+'\n'+Path(path).read_text(encoding='utf-8-sig'))
            self.terms.delete('1.0', 'end'); self.terms.insert('1.0', '\n'.join(terms))
        except (OSError, ValueError) as error:
            messagebox.showerror('无法导入词库', str(error), parent=self.window)

    def set_busy(self, busy):
        self.busy = busy
        state = 'disabled' if busy else 'normal'
        for widget in (self.add_button, self.remove_button, self.import_button, self.start_button, self.project_box, self.terms):
            widget.configure(state=state)

    def start(self):
        if self.busy: return
        if not self.paths:
            self.status.set('请先添加要转写的音频文件。'); return
        try:
            snapshot = self.app.pipeline.vocabulary.snapshot(parse_terms(self.terms.get('1.0', 'end')))
        except (OSError, ValueError) as error:
            messagebox.showerror('无法开始转写', str(error), parent=self.window); return
        paths, project, folder = list(self.paths), self.project.get().strip(), imported_audio_folder(self.root)
        self.set_busy(True)
        self.status.set('正在复制、校验并提交手动转写任务…')
        def work():
            try:
                result = submit_local_batch(self.app.pipeline, paths, folder, snapshot, project,
                                            lambda text: self.events.put(('progress', text)))
            except Exception as error:
                result = ([], [str(error)])
            self.events.put(('done', result))
        # Finish the current verified copy on exit; stop_event prevents later jobs.
        self.worker = threading.Thread(target=work, name='ImportLocalAudio', daemon=False)
        self.worker.start()

    def poll(self):
        if self.closed: return
        while True:
            try: kind, value = self.events.get_nowait()
            except queue.Empty: break
            if kind == 'progress': self.status.set(value)
            else:
                successful, errors = value
                self.paths = [p for p in self.paths if p not in successful]
                self.refresh_files(); self.set_busy(False)
                self.status.set(f'已提交 {len(successful)} 个音频。点击“查看任务与结果”查看进度、取消任务或打开文本。'
                                + (f' {len(errors)} 个失败，仍留在列表中。' if errors else ''))
                if errors and not self.app.closing:
                    messagebox.showerror('部分音频未提交', '\n'.join(errors), parent=self.window)
        self.general.set(f'通用词库：自动加入（当前 {len(self.app.pipeline.vocabulary.rows("accepted"))} 个启用词）')
        try: self.term_count.set(f'去重后 {len(parse_terms(self.terms.get("1.0", "end")))} 个专用词；仅用于本批转写，不修改通用词库。')
        except ValueError as error: self.term_count.set(str(error))
        self.storage.set(f'音频副本：{imported_audio_folder(self.root)}\n文本（年 / 月 / 日）：{transcript_folder(self.root)}')
        with self.app.pipeline.library.lock:
            jobs = [j for j in self.app.pipeline.library.data['jobs'].values() if j.get('kind')=='local']
        pending = sum(j['status'] in ('pending','running') for j in jobs)
        failed = sum(j['status']=='failed' for j in jobs)
        detail = self.app.pipeline.asr_status if any(j['status']=='running' for j in jobs) else (
            self.app.pipeline.last_error if pending or failed else '')
        self.progress.set(f'本地音频任务：{pending} 个进行中 / 等待，{failed} 个失败待重试。 {detail}')
        self.window.after(500, self.poll)

    def close(self):
        # A copy already confirmed by Start continues in the background.
        if self.busy and not self.app.closing:
            self.window.withdraw(); return
        self.closed = True
        self.app.transcription_panel = None
        self.window.destroy()

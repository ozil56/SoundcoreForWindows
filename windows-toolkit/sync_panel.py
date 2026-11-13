"""Settings and progress for scheduled local recording processing."""
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from recorder_files import download_folder
from tool_settings import (read_settings, update_settings, transcript_folder,
                           parse_clock, schedule_label)


class SyncPanel:
    def __init__(self, app, root):
        self.app, self.root = app, Path(root)
        settings = read_settings(root)
        self.window = tk.Toplevel(app.root)
        self.window.title('日常录音 · 自动同步与转写')
        self.window.geometry('820x780')
        self.window.minsize(760, 740)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.closed = False
        outer = ttk.Frame(self.window, padding=20)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='日常录音 · 自动同步与转写', font=('Microsoft YaHei UI',18,'bold')).pack(anchor='w')
        ttk.Label(outer, text='自动处理已选择的录音豆；外部音频请用“转写本地音频”。下方模型和文本目录由两种转写共用。',
                  wraplength=750).pack(anchor='w', pady=(8,12))
        self.auto_sync = tk.BooleanVar(value=settings.get('auto_sync',False))
        self.auto_delete = tk.BooleanVar(value=settings.get('auto_delete',False))
        self.auto_transcribe = tk.BooleanVar(value=settings.get('auto_transcribe',False))
        for label, variable in [('自动下载已完成的录音',self.auto_sync),
                ('完整保存并校验后，自动删除录音豆上的对应原件',self.auto_delete),
                ('自动将已下载的设备录音转写为中文文本（本地处理）',self.auto_transcribe)]:
            ttk.Checkbutton(outer, text=label, variable=variable).pack(anchor='w',pady=3)
        schedule = ttk.LabelFrame(outer,text='每日运行时间 · 按本机时区',padding=10)
        schedule.pack(fill='x',pady=12)
        self.scheduled = tk.BooleanVar(value=settings.get('schedule_enabled',True))
        ttk.Checkbutton(schedule,text='只在以下时间段下载和转写（取消勾选则全天运行）',
                        variable=self.scheduled).pack(anchor='w')
        times = ttk.Frame(schedule); times.pack(fill='x',pady=6)
        self.start = tk.StringVar(value=settings.get('schedule_start','00:00'))
        self.end = tk.StringVar(value=settings.get('schedule_end','06:00'))
        ttk.Label(times,text='开始').pack(side='left')
        ttk.Entry(times,textvariable=self.start,width=8).pack(side='left',padx=8)
        ttk.Label(times,text='结束').pack(side='left')
        ttk.Entry(times,textvariable=self.end,width=8).pack(side='left',padx=8)
        ttk.Label(times,text='24 小时制，如 00:00；支持跨午夜，相同时间表示全天').pack(side='left')
        ttk.Label(schedule,text='电脑需保持开机且程序运行；不会自动唤醒电脑。到结束时间暂停，下一时间段继续。',
                  wraplength=720).pack(anchor='w')
        self.audio_dir = tk.StringVar(value=str(download_folder(root)))
        self.text_dir = tk.StringVar(value=str(transcript_folder(root)))
        for title, variable in [('音频保存目录',self.audio_dir),('文本保存目录（自动创建 年 / 月 / 日）',self.text_dir)]:
            ttk.Label(outer,text=title).pack(anchor='w',pady=(8,2))
            row = ttk.Frame(outer); row.pack(fill='x')
            ttk.Entry(row,textvariable=variable).pack(side='left',fill='x',expand=True)
            ttk.Button(row,text='选择…',command=lambda v=variable:self.choose(v)).pack(side='left',padx=6)
            ttk.Button(row,text='打开',command=lambda v=variable:self.open_folder(v)).pack(side='left')
        ttk.Label(outer,text='文本按录音开始日期归档，例如 2026 / 11 / 13。修改目录不会移动或删除已有文件。',
                  wraplength=750).pack(anchor='w',pady=(5,8))
        model = ttk.LabelFrame(outer,text='本地识别环境',padding=8); model.pack(fill='x')
        self.python = tk.StringVar(value=settings.get('asr_python') or str(self.root/'.asr-venv'/'Scripts'/'python.exe'))
        self.model = tk.StringVar(value=settings.get('asr_model_dir') or str(self.root/'models'/'Qwen3-ASR-1.7B-hf'))
        for label, variable, is_file in [('运行程序',self.python,True),('模型目录',self.model,False)]:
            row = ttk.Frame(model); row.pack(fill='x',pady=2)
            ttk.Label(row,text=label,width=9).pack(side='left')
            ttk.Entry(row,textvariable=variable).pack(side='left',fill='x',expand=True)
            ttk.Button(row,text='选择…',command=lambda v=variable,f=is_file:self.choose(v,f)).pack(side='left',padx=6)
        ttk.Label(model,text='使用 Qwen3-ASR-1.7B 本地模型；队列完成后自动释放显存。',wraplength=700).pack(anchor='w')
        self.status = tk.StringVar()
        ttk.Label(outer,textvariable=self.status,wraplength=750,justify='left').pack(anchor='w',pady=12)
        buttons = ttk.Frame(outer); buttons.pack(side='bottom',fill='x')
        ttk.Button(buttons,text='保存设置',command=self.save).pack(side='left')
        ttk.Button(buttons,text='重新检查队列',command=app.pipeline.request_scan).pack(side='left',padx=8)
        ttk.Button(buttons,text='关闭',command=self.close).pack(side='right')
        self.poll()

    def choose(self, variable, is_file=False):
        if is_file:
            value = filedialog.askopenfilename(parent=self.window,title='选择本地识别环境的 python.exe',
                                               filetypes=[('Python','python.exe'),('所有文件','*.*')])
        else:
            value = filedialog.askdirectory(parent=self.window,initialdir=variable.get() if Path(variable.get()).is_dir() else str(self.root))
        if value:
            variable.set(value)

    def open_folder(self, variable):
        try:
            folder = Path(variable.get()).resolve()
            folder.mkdir(parents=True,exist_ok=True)
            os.startfile(folder)
        except OSError as error:
            messagebox.showerror('无法打开目录',str(error),parent=self.window)

    def save(self):
        try:
            for value in (self.start.get(), self.end.get()):
                parse_clock(value)
            if not self.audio_dir.get().strip() or not self.text_dir.get().strip():
                raise ValueError('请选择音频和文本目录')
            audio, text = Path(self.audio_dir.get()).resolve(), Path(self.text_dir.get()).resolve()
            if audio == text:
                raise ValueError('请为文本选择与音频不同的目录')
            if self.auto_transcribe.get():
                model = Path(self.model.get())
                if not Path(self.python.get()).is_file() or not (model/'config.json').is_file() or not list(model.glob('*.safetensors')):
                    raise ValueError('本地识别环境尚未就绪，请先安装或选择运行程序和模型目录')
            for folder in (audio, text):
                folder.mkdir(parents=True,exist_ok=True)
            update_settings(self.root,auto_sync=self.auto_sync.get(),auto_delete=self.auto_delete.get(),
                auto_transcribe=self.auto_transcribe.get(),schedule_enabled=self.scheduled.get(),
                schedule_start=self.start.get(),schedule_end=self.end.get(),download_dir=str(audio),
                transcript_dir=str(text),asr_python=self.python.get(),asr_model_dir=self.model.get())
            self.app.pipeline.request_scan()
            self.app.message('自动同步与转写设置已保存：'+schedule_label(read_settings(self.root)))
        except (OSError,ValueError) as error:
            messagebox.showerror('无法保存设置',str(error),parent=self.window)

    def poll(self):
        if self.closed:
            return
        state = self.app.pipeline.snapshot()
        counts = state['counts']
        self.status.set(f"下载：{state['sync_status']}（{state['download_progress']:.0f}%）\n"
                        f"转写：{state['asr_status']}（{state['transcribe_progress']:.0f}%）\n"
                        f"队列：待处理 {counts['pending']} · 处理中 {counts['running']} · 已完成 {counts['complete']} · 待重试 {counts['failed']}")
        self.window.after(700,self.poll)

    def close(self):
        self.closed = True
        self.window.destroy()
        self.app.sync_panel = None

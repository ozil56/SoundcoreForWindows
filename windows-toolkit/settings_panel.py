from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk,filedialog
import webbrowser

from ui_common import Panel,open_folder
from tool_settings import read_settings,update_settings,parse_clock,imported_audio_folder,transcript_folder
from recorder_files import download_folder
from vocabulary import VocabularyStore,ProjectVocabularies
from ollama_summary import DEFAULT_URL,DEFAULT_MODEL,models,server_url


class SettingsPanel(Panel):
    def __init__(self,app,root):
        super().__init__(app,'设置','settings_panel',860,650)
        self.root=Path(root);self.values={};self.events=queue.Queue()
        settings=read_settings(root)
        tabs=ttk.Notebook(self.body);tabs.pack(fill='both',expand=True)
        storage=ttk.Frame(tabs,padding=14);tabs.add(storage,text='目录与本地模型')
        plan=ttk.Frame(tabs,padding=18);tabs.add(plan,text='自动处理与启动')
        ollama=ttk.Frame(tabs,padding=18);tabs.add(ollama,text='Ollama 标题与纪要')
        decoding=ttk.Frame(tabs,padding=18);tabs.add(decoding,text='识别参数')
        for key,label,default,low,high,step in [('asr_chunk_seconds','每段音频最长（秒）',60,15,180,15),
                ('asr_max_new_tokens','单段输出上限（tokens）',4096,512,8192,512)]:
            ttk.Label(decoding,text=label).pack(anchor='w',pady=(14,5))
            value=tk.StringVar(value=str(settings.get(key,default)));self.values[key]=value
            ttk.Spinbox(decoding,from_=low,to=high,increment=step,textvariable=value,width=16).pack(anchor='w')
        ttk.Label(decoding,text='按停顿组织语音段，实际长度通常小于上限。长段可保留更多上下文；\n输出上限只限制最长生成，不会强制生成这么多文字。\n重复输出会提前检测，问题片段自动细分重试，不接受截断结果。\n已有未完成任务保留其分段进度；新任务使用新的分段设置。',wraplength=730).pack(anchor='w',pady=24)
        def field(parent,key,label,default,file=False):
            value=tk.StringVar(value=settings.get(key,default));self.values[key]=value
            ttk.Label(parent,text=label).pack(anchor='w',pady=(6,2))
            row=ttk.Frame(parent);row.pack(fill='x')
            ttk.Entry(row,textvariable=value).pack(side='left',fill='x',expand=True)
            ttk.Button(row,text='选择…',command=lambda:self.choose(value,file)).pack(side='left',padx=(8,0))
        field(storage,'download_dir','设备录音下载目录',str(download_folder(root)))
        field(storage,'import_dir','自助转写音频副本目录',str(imported_audio_folder(root)))
        field(storage,'transcript_dir','转写目录（按 年 / 月 / 日 保存）',str(transcript_folder(root)))
        field(storage,'vocabulary_dir','词库目录',str(app.pipeline.vocabulary.folder))
        field(storage,'asr_python','本地转写运行程序',str(Path(root)/'.asr-venv/Scripts/python.exe'),True)
        field(storage,'asr_model_dir','语音识别模型目录',str(Path(root)/'models/Qwen3-ASR-1.7B-hf'))
        ttk.Label(storage,text='目录变更用于后续保存；已有录音、结果和模型不会自动移动。',foreground='#666666').pack(anchor='w',pady=10)
        for key,label,default in [('auto_sync','自动下载设备录音',False),('auto_delete','下载完整并校验后，自动删除设备原件',False),
                ('auto_transcribe','自动转写已下载录音',False),('schedule_enabled','仅在以下时间段自动处理',True)]:
            var=tk.BooleanVar(value=settings.get(key,default));self.values[key]=var
            ttk.Checkbutton(plan,text=label,variable=var).pack(anchor='w',pady=8)
        row=ttk.Frame(plan);row.pack(fill='x',pady=12)
        for key,label,default in [('schedule_start','开始','00:00'),('schedule_end','结束','06:00')]:
            ttk.Label(row,text=label).pack(side='left',padx=(0,8))
            var=tk.StringVar(value=settings.get(key,default));self.values[key]=var
            ttk.Entry(row,textvariable=var,width=10).pack(side='left',padx=(0,20))
        ttk.Label(plan,text='24 小时制，支持跨天，例如 20:00–08:00；相同时间表示全天。\n立即处理和自助转写不受该时段限制。电脑需保持开机。',wraplength=740).pack(anchor='w',pady=10)
        self.startup=tk.BooleanVar(value=app.autostart.enabled())
        ttk.Checkbutton(plan,text='登录 Windows 后自动启动并驻留托盘',variable=self.startup).pack(anchor='w',pady=16)
        enabled=tk.BooleanVar(value=settings.get('ollama_enabled',False));self.values['ollama_enabled']=enabled
        ttk.Checkbutton(ollama,text='生成标题和纪要',variable=enabled).pack(anchor='w',pady=8)
        for key,label,default in [('ollama_url','服务器地址',DEFAULT_URL),('ollama_model','模型名称',DEFAULT_MODEL)]:
            ttk.Label(ollama,text=label).pack(anchor='w',pady=(10,3))
            var=tk.StringVar(value=settings.get(key,default));self.values[key]=var
            ttk.Entry(ollama,textvariable=var).pack(fill='x')
        row=ttk.Frame(ollama);row.pack(fill='x',pady=18)
        ttk.Label(row,text='录音达到').pack(side='left')
        self.minimum=tk.StringVar(value=str(float(settings.get('ollama_min_seconds',120))/60))
        ttk.Spinbox(row,from_=0,to=120,increment=.5,textvariable=self.minimum,width=8).pack(side='left',padx=8)
        ttk.Label(row,text='分钟后才生成标题和纪要').pack(side='left')
        ttk.Label(ollama,text='短录音直接保存转写；有效文字很少时也跳过。原始转写始终保留。\n纪要包含：标题、主要内容、待办事项、完整正文润色。\n转写保存为 TXT；纪要保存为 Markdown（.md），含标题、分节和列表。\n文件名：20261111-152313-会议标题-纪要.md。\n启用后，转写文字会发送到你配置的 Ollama 服务器。',wraplength=735).pack(anchor='w',pady=10)
        ttk.Button(ollama,text='测试连接与模型',command=self.test_connection).pack(anchor='w')
        self.status=tk.StringVar(value='设置保存后生效。')
        ttk.Label(self.body,textvariable=self.status,wraplength=790).pack(anchor='w',pady=12)
        bar=ttk.Frame(self.body);bar.pack(fill='x')
        ttk.Button(bar,text='保存设置',command=self.save).pack(side='left')
        ttk.Label(bar,text='v'+app.version).pack(side='left',padx=12)
        link=ttk.Label(bar,text='github.com/ozil56/SoundcoreForWindows',foreground='#1769aa',cursor='hand2');link.pack(side='left')
        link.bind('<Button-1>',lambda _:webbrowser.open('https://github.com/ozil56/SoundcoreForWindows'))
        ttk.Button(bar,text='关闭',command=self.close).pack(side='right')
        self.poll()

    def choose(self,var,file):
        value=filedialog.askopenfilename(parent=self.window,filetypes=[('Python','python.exe')]) if file else filedialog.askdirectory(parent=self.window)
        if value:var.set(value)

    def test_connection(self):
        url=self.values['ollama_url'].get();model=self.values['ollama_model'].get().strip()
        self.status.set('正在检测 Ollama…')
        def work():
            try:
                found=models(url)
                self.events.put('连接成功，模型已就绪。' if model in found else '服务器已连接，但未找到模型：'+model)
            except Exception as e:self.events.put('连接失败：'+str(e))
        threading.Thread(target=work,daemon=True).start()

    def save(self):
        try:
            values={key:var.get() for key,var in self.values.items()}
            for key in ('schedule_start','schedule_end'):parse_clock(values[key])
            values['ollama_url']=server_url(values['ollama_url'])
            values['ollama_model']=values['ollama_model'].strip()
            values['ollama_min_seconds']=float(self.minimum.get())*60
            values['asr_chunk_seconds']=int(values['asr_chunk_seconds'])
            values['asr_max_new_tokens']=int(values['asr_max_new_tokens'])
            if not 15<=values['asr_chunk_seconds']<=180 or not 512<=values['asr_max_new_tokens']<=8192:
                raise ValueError('音频段长应为 15–180 秒；输出上限应为 512–8192 tokens')
            if not 0<=values['ollama_min_seconds']<=7200:raise ValueError('时长阈值应为 0–120 分钟')
            if not values['ollama_model']:raise ValueError('请输入 Ollama 模型名')
            for key in ('download_dir','import_dir','transcript_dir','vocabulary_dir'):
                if not values[key].strip():raise ValueError('目录不能为空')
                values[key]=str(Path(values[key]).resolve());Path(values[key]).mkdir(parents=True,exist_ok=True)
            if values['download_dir']==values['transcript_dir']:raise ValueError('录音和转写文本请选择不同目录')
            if values['auto_transcribe']:
                if not Path(values['asr_python']).is_file() or not (Path(values['asr_model_dir'])/'config.json').is_file():
                    raise ValueError('本地转写运行程序或模型目录无效')
            self.app.autostart.set_enabled(self.startup.get())
            update_settings(self.root,**values)
            self.app.pipeline.vocabulary=VocabularyStore(self.root)
            self.app.pipeline.projects=ProjectVocabularies(self.root)
            self.app.pipeline.request_scan()
            self.status.set('设置已保存。')
        except Exception as e:self.error(e)

    def poll(self):
        if self.closed:return
        while not self.events.empty():self.status.set(self.events.get_nowait())
        self.window.after(300,self.poll)

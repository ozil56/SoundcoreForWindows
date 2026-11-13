import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk,filedialog

from ui_common import Panel
from local_transcription_panel import submit_local_batch,AUDIO_TYPES
from tool_settings import imported_audio_folder
from recorder_files import download_folder
from vocabulary import parse_terms


class SelfServicePanel(Panel):
    def __init__(self,app,root):
        super().__init__(app,'自助转写','transcription_panel',800,550)
        self.root=Path(root);self.paths=[];self.keys=[];self.events=queue.Queue();self.busy=False
        with app.pipeline.library.lock:
            self.keys=[k for k in app.pipeline.library.data.get('recent_local_batch',[]) if k in app.pipeline.library.data['jobs']]
        self.imported_terms=[];self.outputs=[];self.worker=None
        self.cancel_requested=False
        row=ttk.Frame(self.body);row.pack(fill='x')
        ttk.Button(row,text='选择录音文件…',command=self.choose).pack(side='left')
        ttk.Button(row,text='移除所选',command=self.remove).pack(side='left',padx=8)
        self.files=tk.Listbox(self.body,height=6,selectmode='extended',exportselection=False)
        self.files.pack(fill='x',pady=10)
        ttk.Label(self.body,text='可选择设备下载目录或其他录音设备的音频；原始文件和已有转写结果始终保留。',wraplength=750,foreground='#666666').pack(anchor='w')
        self.general=tk.BooleanVar(value=True)
        ttk.Checkbutton(self.body,text='使用通用词库',variable=self.general).pack(anchor='w',pady=12)
        row=ttk.Frame(self.body);row.pack(fill='x')
        ttk.Label(row,text='专用词库').pack(side='left')
        self.special=tk.StringVar(value='不使用')
        self.project_box=ttk.Combobox(row,textvariable=self.special,state='readonly',width=38,postcommand=self.refresh_projects)
        self.project_box.pack(side='left',padx=12)
        ttk.Button(row,text='临时导入 TXT…',command=self.import_terms).pack(side='left')
        self.refresh_projects()
        ttk.Label(self.body,text='专用词库可在“词库管理”中按项目维护；TXT 格式为 UTF-8，每行一个词或短语。',wraplength=750,foreground='#666666').pack(anchor='w',pady=10)
        row=ttk.Frame(self.body);row.pack(fill='x',pady=12)
        self.start_button=ttk.Button(row,text='开始转写',command=self.start);self.start_button.pack(side='left')
        ttk.Button(row,text='取消本次未完成任务',command=self.cancel).pack(side='left',padx=12)
        self.percent=tk.DoubleVar();ttk.Progressbar(self.body,variable=self.percent,maximum=100).pack(fill='x')
        self.status=tk.StringVar(value='请选择录音和可选词库，然后开始转写。')
        ttk.Label(self.body,textvariable=self.status,wraplength=750).pack(anchor='w',pady=10)
        row=ttk.Frame(self.body);row.pack(fill='x')
        self.result=tk.StringVar()
        self.result_box=ttk.Combobox(row,textvariable=self.result,state='readonly',width=64);self.result_box.pack(side='left',fill='x',expand=True)
        self.open_button=ttk.Button(row,text='打开转写结果',command=self.open_result,state='disabled');self.open_button.pack(side='left',padx=8)
        self.poll()

    def refresh_projects(self):
        self.project_map={p['name']:p['id'] for p in self.app.pipeline.projects.rows() if p['state']=='accepted'}
        options=['不使用',*self.project_map]
        if self.imported_terms:options.append(f'临时词库（{len(self.imported_terms)} 词）')
        self.project_box.configure(values=options)
        if self.special.get() not in options:self.special.set('不使用')

    def choose(self):
        if self.busy:return
        paths=filedialog.askopenfilenames(parent=self.window,title='选择录音文件',initialdir=str(download_folder(self.root)),filetypes=AUDIO_TYPES)
        self.add_files(paths)

    def add_files(self,paths):
        for path in paths:
            path=str(Path(path).resolve())
            if path not in self.paths:self.paths.append(path)
        self.refresh_files()

    def refresh_files(self):
        self.files.delete(0,'end')
        for path in self.paths:self.files.insert('end',path)

    def remove(self):
        if self.busy:return
        for index in reversed(self.files.curselection()):del self.paths[index]
        self.refresh_files()

    def import_terms(self):
        path=filedialog.askopenfilename(parent=self.window,filetypes=[('UTF-8 词库','*.txt')])
        if not path:return
        try:
            self.imported_terms=parse_terms(Path(path).read_text(encoding='utf-8-sig'))
            self.refresh_projects()
            if self.imported_terms:self.special.set(f'临时词库（{len(self.imported_terms)} 词）')
        except Exception as e:self.error(e)

    def start(self):
        if self.busy:return
        if not self.paths:self.status.set('请先选择录音文件。');return
        try:
            self.app.pipeline.runtime()
            project=self.special.get() if self.special.get() in self.project_map else ''
            terms=self.app.pipeline.projects.terms(self.project_map[project]) if project else (
                self.imported_terms if self.special.get().startswith('临时词库') else [])
            snapshot=self.app.pipeline.vocabulary.snapshot(terms,include_general=self.general.get())
        except Exception as e:self.error(e);return
        self.busy=True;self.start_button.configure(state='disabled');self.keys=[];self.outputs=[]
        self.cancel_requested=False
        with self.app.pipeline.library.lock:
            self.app.pipeline.library.data['recent_local_batch']=[];self.app.pipeline.library.save()
        paths=list(self.paths)
        def submitted(key):
            with self.app.pipeline.library.lock:
                batch=self.app.pipeline.library.data['recent_local_batch']
                if key not in batch:batch.append(key);self.app.pipeline.library.save()
            self.events.put(('key',key))
        def work():
            try:
                result=submit_local_batch(self.app.pipeline,paths,imported_audio_folder(self.root),snapshot,project,
                    lambda message:self.events.put(('status',message)),submitted)
                self.events.put(('done',result))
            except Exception as e:self.events.put(('error',str(e)))
        self.worker=threading.Thread(target=work,name='ImportLocalAudio',daemon=False);self.worker.start()

    def cancel(self):
        self.cancel_requested=True
        for key in self.keys:self.app.pipeline.library.cancel(key)
        self.status.set('已取消未完成任务；已完成结果保留。')

    def open_result(self):
        index=self.result_box.current()
        if 0<=index<len(self.outputs):os.startfile(self.outputs[index])

    def poll(self):
        if self.closed:return
        while not self.events.empty():
            kind,value=self.events.get_nowait()
            if kind=='key':
                if value not in self.keys:self.keys.append(value)
                if self.cancel_requested:self.app.pipeline.library.cancel(value)
            elif kind=='done':
                self.busy=False;self.start_button.configure(state='normal')
                self.status.set(f'已提交 {len(value[0])} 条录音'+('；'+value[1][0] if value[1] else ''))
            elif kind=='error':self.busy=False;self.start_button.configure(state='normal');self.status.set(value)
            else:self.status.set(value)
        if self.keys:
            pipeline=self.app.pipeline
            with pipeline.library.lock:jobs=[pipeline.library.data['jobs'][k].copy() for k in self.keys]
            done=sum(j['status']=='complete' for j in jobs)
            running=any(j['status']=='running' for j in jobs)
            failed=next((j for j in jobs if j['status']=='failed'),None)
            percent=(done*100+(pipeline.transcribe_progress if running else 0))/len(jobs)
            self.percent.set(percent)
            post=any(j.get('enrichment_state') in ('pending','running') for j in jobs)
            self.status.set((f'已完成 {done}/{len(jobs)} 条 · {percent:.0f}%'+(' · '+pipeline.post_status if post else '')) if not failed else
                '转写暂未完成：'+failed.get('error','等待重试')[:130])
            if any(j.get('enrichment_state')=='failed' for j in jobs) and not failed:
                self.status.set(self.status.get()+' · 纪要稍后重试，转写结果已保存')
            outputs=[]
            for job in jobs:
                if job['status']=='complete':
                    outputs.append(job.get('named_output') or job['output'])
                    if job.get('minutes_output'):outputs.append(job['minutes_output'])
            if outputs!=self.outputs:
                self.outputs=outputs;self.result_box.configure(values=[Path(p).name for p in outputs])
                if outputs:self.result_box.current(0)
            self.open_button.configure(state='normal' if outputs else 'disabled')
        self.window.after(500,self.poll)

    def close(self):
        if self.busy and not self.app.closing:self.window.withdraw();return
        super().close()

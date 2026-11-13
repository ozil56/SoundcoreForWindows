import asyncio
import datetime as dt
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from ui_common import Panel,open_folder
from bridge_setup import environment_summary
from recorder_files import Candidate,load_selection,save_selection,scan
from tool_settings import read_settings,in_schedule,parse_clock,transcript_folder


class ConnectionPanel(Panel):
    def __init__(self,app,root):
        super().__init__(app,'连接录音豆','connection_panel',820,570)
        self.root=Path(root);self.events=queue.Queue();self.candidates={};self.busy=False
        self.audio=tk.StringVar();self.detect()
        row=ttk.Frame(self.body);row.pack(fill='x')
        ttk.Label(row,textvariable=self.audio,wraplength=610).pack(side='left')
        ttk.Button(row,text='重新检测',command=self.detect).pack(side='right')
        ttk.Label(self.body,text='使用 Typeless 时选择 CABLE Output；录音下载与本地转写不依赖虚拟声卡。',foreground='#666666').pack(anchor='w',pady=10)
        self.status=tk.StringVar();ttk.Label(self.body,textvariable=self.status,font=('Microsoft YaHei UI',11)).pack(anchor='w',pady=12)
        self.devices=ttk.Treeview(self.body,columns=('name','id','state'),show='headings',height=8,selectmode='browse')
        for key,label,width in [('name','录音豆',230),('id','设备标识',190),('state','状态',280)]:
            self.devices.heading(key,text=label);self.devices.column(key,width=width)
        self.devices.pack(fill='both',expand=True)
        self.show_candidates([])
        bar=ttk.Frame(self.body);bar.pack(fill='x',pady=12)
        self.scan_button=ttk.Button(bar,text='搜索录音豆',command=self.search);self.scan_button.pack(side='left')
        ttk.Button(bar,text='选择并连接',command=self.select).pack(side='left',padx=8)
        ttk.Button(bar,text='断开',command=self.disconnect).pack(side='left',padx=8)
        ttk.Button(bar,text='重连',command=self.reconnect).pack(side='left')
        self.notice=tk.StringVar(value='自动连接始终开启；断开后等待 30 秒再自动重连。')
        ttk.Label(self.body,textvariable=self.notice,wraplength=760).pack(anchor='w')
        self.poll()

    def detect(self):
        text=environment_summary(self.root).splitlines()[1]
        if '未就绪' in text:text='虚拟声卡：当前会话未检测到完整端口（远程桌面可能不可用）'
        self.audio.set(text)

    def show_candidates(self,items):
        remembered=load_selection(self.root)
        self.candidates={c.identity_hex:c for c in items}
        if remembered:
            key=remembered['identity_hex']
            self.candidates.setdefault(key,Candidate(key,remembered.get('name','soundcore Work'),-999,False))
        self.devices.delete(*self.devices.get_children())
        for key,item in self.candidates.items():
            self.devices.insert('','end',iid=key,values=(item.name,key.upper(),
                '已选择' if remembered and key==remembered['identity_hex'] else '附近设备'))
        if remembered:self.devices.selection_set(remembered['identity_hex'])

    def search(self):
        if self.busy:return
        self.busy=True;self.scan_button.configure(state='disabled');self.notice.set('搜索中，约 10 秒…')
        def work():
            try:self.events.put(('devices',asyncio.run(scan())))
            except Exception as e:self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()

    def select(self):
        rows=self.devices.selection()
        if not rows:self.notice.set('请先选择一个录音豆。');return
        if self.app.manager:self.notice.set('请先关闭设备录音管理面板，再切换录音豆。');return
        try:
            save_selection(self.root,self.candidates[rows[0]])
            self.app.pipeline.device_pending=None;self.app.pipeline.next_inventory=0
            self.app.service.command('reconnect');self.notice.set('已记住选择，正在自动连接。')
        except Exception as e:self.error(e)

    def disconnect(self):
        if self.app.manager:self.notice.set('请先关闭设备录音管理面板。');return
        self.app.service.command('disconnect')

    def reconnect(self):
        if self.app.manager:self.notice.set('请先关闭设备录音管理面板。');return
        self.app.service.command('reconnect')

    def poll(self):
        if self.closed:return
        while not self.events.empty():
            kind,value=self.events.get_nowait();self.busy=False;self.scan_button.configure(state='normal')
            if kind=='devices':self.show_candidates(value);self.notice.set(f'发现 {len(value)} 个设备；已选择的录音豆始终保留在列表中。')
            else:self.notice.set(value)
        status=self.app.service.snapshot()['status']
        if not load_selection(self.root):status='尚未选择录音豆，请搜索并选择。'
        if self.app.manager:status='设备录音管理正在使用连接，关闭后恢复常驻连接。'
        self.status.set(status)
        self.window.after(700,self.poll)


def automatic_summary(pipeline,settings,now=None):
    now=now or dt.datetime.now().astimezone()
    with pipeline.library.lock:
        jobs=list(pipeline.library.data['jobs'].values())
        unfinished=[j for j in jobs if j['status'] in ('pending','running','failed')]
        unique={j.get('record_id') or f"{j['receipt']['identity_hex']}:{j['receipt']['file_id']}:{j['receipt']['sha256']}" for j in unfinished}
        completed=[j.get('completed_at') for j in jobs if not j.get('manual') and j['status']=='complete' and j.get('completed_at')]
        post=[j for j in jobs if j.get('enrichment_state') in ('pending','running','failed')]
    last=max(completed,default=None)
    if last:
        stamp=dt.datetime.fromisoformat(last);minutes=max(0,int((now-stamp).total_seconds()/60))
        age=f'{minutes} 分钟前' if minutes<60 else f'{minutes//60} 小时 {minutes%60} 分钟前'
        last_text=f'{stamp:%m-%d %H:%M}（{age}）'
    else:last_text='暂无自动转写完成记录'
    if any(j['status']=='running' for j in unfinished) or pipeline.post_status:expected='正在处理'
    elif any(j.get('manual') or j.get('run_immediately') for j in unfinished) or pipeline.immediate_identity:expected='立即处理；设备录音等待连接和空闲'
    elif not (settings.get('auto_transcribe') or settings.get('auto_sync')):expected='自动处理已关闭，可点击下方按钮立即处理'
    elif in_schedule(settings,now):expected='当前在运行时段，设备空闲后自动处理'
    else:
        minute=parse_clock(settings.get('schedule_start','00:00'))
        start=now.replace(hour=minute//60,minute=minute%60,second=0,microsecond=0)
        if start<=now:start+=dt.timedelta(days=1)
        expected=f'{start:%m-%d %H:%M} 开始（电脑开机、程序运行、设备可连接）'
    return {'last':last_text,'local':len(unique),'tasks':len(unfinished),'post':len(post),'expected':expected,
        'post_failed':sum(j.get('enrichment_state')=='failed' for j in post),
        'failed':sum(j['status']=='failed' for j in unfinished)}


class AutomaticPanel(Panel):
    def __init__(self,app,root):
        super().__init__(app,'自动转写','automatic_panel',800,470)
        self.root=Path(root);self.info=tk.StringVar();self.status=tk.StringVar();self.progress=tk.DoubleVar()
        ttk.Label(self.body,textvariable=self.info,justify='left',font=('Microsoft YaHei UI',11),wraplength=750).pack(anchor='w',pady=(0,20))
        ttk.Progressbar(self.body,variable=self.progress,maximum=100).pack(fill='x')
        ttk.Label(self.body,textvariable=self.status,wraplength=750).pack(anchor='w',pady=12)
        row=ttk.Frame(self.body);row.pack(fill='x',pady=16)
        ttk.Button(row,text='立即下载全部录音并转写',command=app.sync_device_now).pack(side='left')
        ttk.Button(row,text='立即转写全部剩余本地录音',command=self.local_now).pack(side='left',padx=12)
        ttk.Button(self.body,text='打开转写目录',command=lambda:open_folder(transcript_folder(root))).pack(anchor='w')
        ttk.Label(self.body,text='本页只显示数量与进度。专用词库重新转写，请在“自助转写”选择已下载的音频。',wraplength=750,foreground='#666666').pack(anchor='w',pady=16)
        self.poll()

    def local_now(self):
        try:self.app.pipeline.request_local_transcription()
        except Exception as e:self.error(e)

    def poll(self):
        if self.closed:return
        pipeline=self.app.pipeline;state=automatic_summary(pipeline,read_settings(self.root))
        checked=dt.datetime.fromisoformat(pipeline.device_checked_at).strftime('%H:%M') if pipeline.device_checked_at else ''
        pending='等待设备连接后统计' if pipeline.device_pending is None else f'{pipeline.device_pending} 条（{checked} 读取）'
        self.info.set(f'上次自动转写完成：{state["last"]}\n\n设备中尚未下载：{pending}\n本地待完成转写：{state["local"]} 条录音 / {state["tasks"]} 个任务\n待生成标题与纪要：{state["post"]} 个\n\n预计运行：{state["expected"]}')
        busy=pipeline.active_job is not None
        self.progress.set(pipeline.transcribe_progress if busy else 0)
        if not busy and self.app.service.snapshot()['phase']=='syncing':self.progress.set(pipeline.download_progress)
        self.status.set(pipeline.post_status or (f'{pipeline.asr_status}（{pipeline.transcribe_progress:.0f}%）' if busy else pipeline.sync_status))
        if state['failed']:self.status.set(self.status.get()+f'；{state["failed"]} 个失败任务等待重试')
        elif state['post_failed']:self.status.set(self.status.get()+'；纪要稍后重试，原始转写已保存')
        self.window.after(700,self.poll)

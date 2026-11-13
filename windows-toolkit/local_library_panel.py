"""Local audio library; opening this panel never takes ownership of Bluetooth."""
import datetime as dt
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from auto_sync import recording_date
from recorder_files import download_folder
from vocabulary import parse_terms
from vocabulary_panel import terms_dialog


class LocalLibraryPanel:
    def __init__(self,app,root):
        self.app=app; self.root=Path(root); self.library=app.pipeline.library; self.closed=False
        self.events=queue.Queue(); self.signature=None; self.rows={}
        self.window=tk.Toplevel(app.root); self.window.title('录音管理 · 本地录音与转写版本')
        self.window.geometry('1100x790'); self.window.minsize(900,650)
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        frame=ttk.Frame(self.window,padding=14); frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='本地音频可随时重新转写，无需连接录音豆。旧版本会一直保留。').pack(anchor='w')
        bar=ttk.Frame(frame); bar.pack(fill='x',pady=8)
        for label,cmd in [('用专用词库重新转写',self.rerun),('项目归属',self.assign),('重命名',self.rename),('导入音频',self.import_audio),('设备内录音',lambda:app.show_manager('files'))]:
            ttk.Button(bar,text=label,command=cmd).pack(side='left',padx=(0,6))
        self.query=tk.StringVar(); ttk.Entry(bar,textvariable=self.query,width=18).pack(side='right')
        ttk.Label(bar,text='搜索录音 / 项目 ').pack(side='right')
        self.query.trace_add('write',lambda *_:self.refresh(force=True))
        records_frame=ttk.Frame(frame); records_frame.pack(fill='x')
        self.tree=ttk.Treeview(records_frame,columns=('date','title','project','state','versions'),show='headings',height=9,selectmode='extended')
        for key,label,width in [('date','录音日期',140),('title','录音',380),('project','项目',170),('state','转写状态',120),('versions','版本数',65)]:
            self.tree.heading(key,text=label); self.tree.column(key,width=width)
        record_scroll=ttk.Scrollbar(records_frame,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=record_scroll.set); record_scroll.pack(side='right',fill='y')
        self.tree.pack(fill='x'); self.tree.bind('<<TreeviewSelect>>',lambda _:self.details())
        self.tree.bind('<Double-1>',lambda _:self.rerun())
        vbar=ttk.Frame(frame); vbar.pack(fill='x',pady=8)
        ttk.Label(vbar,text='结果版本').pack(side='left')
        self.version=tk.StringVar(); self.version_box=ttk.Combobox(vbar,textvariable=self.version,state='readonly',width=53)
        self.version_box.pack(side='left',padx=8); self.version_box.bind('<<ComboboxSelected>>',lambda _:self.preview())
        for label,cmd in [('设为当前版本',self.set_current),('打开文本',self.open_text),('音频目录',self.open_audio)]:
            ttk.Button(vbar,text=label,command=cmd).pack(side='left',padx=(0,5))
        self.info=tk.StringVar(); ttk.Label(frame,textvariable=self.info,wraplength=1020).pack(anchor='w')
        text_frame=ttk.Frame(frame); text_frame.pack(fill='both',expand=True,pady=8)
        self.text=tk.Text(text_frame,wrap='word',height=16,font=('Microsoft YaHei UI',10),state='disabled')
        text_scroll=ttk.Scrollbar(text_frame,orient='vertical',command=self.text.yview)
        self.text.configure(yscrollcommand=text_scroll.set); text_scroll.pack(side='right',fill='y')
        self.text.pack(fill='both',expand=True)
        actions=ttk.Frame(frame); actions.pack(fill='x')
        ttk.Button(actions,text='提交纠正词 → 新词审核',command=self.correct).pack(side='left')
        ttk.Button(actions,text='本次词库 → 挑选新词',command=self.suggest).pack(side='left',padx=8)
        ttk.Button(actions,text='取消选中录音的待办任务',command=self.cancel).pack(side='right')
        self.status=tk.StringVar(); ttk.Label(frame,textvariable=self.status,wraplength=1030).pack(anchor='w',pady=(8,0))
        self.refresh(True); self.poll()

    def selected(self):
        ids=self.tree.selection()
        return self.rows.get(ids[0]) if ids else None

    def refresh(self,force=False):
        rows=self.library.records()
        with self.library.lock: jobs=json.loads(json.dumps(self.library.data['jobs']))
        signature=json.dumps([rows,[(k,j['status']) for k,j in jobs.items()]],ensure_ascii=False)
        if signature==self.signature and not force: return
        self.signature=signature; self.rows={r['id']:r for r in rows}
        selected=self.tree.selection(); self.tree.delete(*self.tree.get_children())
        states={'pending':'等待转写','running':'转写中','complete':'已完成','failed':'失败待重试','cancelled':'已取消'}
        for record in sorted(rows,key=lambda r:recording_date(r['receipt'])[0],reverse=True):
            if self.query.get().lower() not in (record['title']+' '+record['project']).lower(): continue
            related=[j for k,j in jobs.items() if j.get('record_id',k)==record['id']]
            active=next((j for j in related if j['status']=='running'),None) or next((j for j in reversed(related) if j['status'] in ('pending','failed')),None)
            state=states[active['status']] if active else ('已完成' if record['versions'] else '已取消')
            self.tree.insert('', 'end',iid=record['id'],values=(recording_date(record['receipt'])[0].strftime('%Y-%m-%d %H:%M'),
                record['title'],record['project'] or '未归类',state,len(record['versions'])))
        for item in selected:
            if self.tree.exists(item): self.tree.selection_add(item)
        self.details()

    def details(self):
        record=self.selected(); self.versions=record['versions'] if record else []
        labels=[f"{'★ ' if v['id']==record['current_version'] else ''}{i+1}. {v['label']} · {v['created_at']}" for i,v in enumerate(self.versions)]
        old=self.version_box.current(); self.version_box['values']=labels
        if labels:
            index=next((i for i,v in enumerate(self.versions) if v['id']==record['current_version']),len(labels)-1)
            self.version_box.current(index)
        else: self.version.set('尚无结果')
        self.preview()

    def selected_version(self):
        index=self.version_box.current()
        return self.versions[index] if 0<=index<len(self.versions) else None

    def preview(self):
        version=self.selected_version(); content='请选择一条本地录音。'
        if version:
            try: content=Path(version['output']).read_text(encoding='utf-8-sig')
            except OSError as e: content=f'文本暂不可用：{e}'
            vocab=version.get('vocabulary',{})
            self.info.set(f"通用词 {len(vocab.get('general',[]))} 个 · 专用词 {len(vocab.get('special',[]))} 个 · {version['output']}")
        else: self.info.set('自动转写按既定时间运行；手动重新转写立即排队，不受时间段限制。')
        self.text.configure(state='normal'); self.text.delete('1.0','end'); self.text.insert('1.0',content); self.text.configure(state='disabled')

    def rerun(self):
        record=self.selected()
        if not record: return
        project=tk.StringVar(value=record['project'])
        def extra(frame):
            ttk.Label(frame,text=f"录音：{record['title']}\n通用词库自动加入；填写下方补充词，新结果另存为一个版本。").pack(anchor='w',pady=8)
            row=ttk.Frame(frame); row.pack(fill='x')
            ttk.Label(row,text='项目（可留空）').pack(side='left')
            ttk.Combobox(row,textvariable=project,values=self.library.data['projects'],width=38).pack(side='left',padx=8)
        def submit(terms):
            self.library.edit_record([record['id']],project=project.get())
            self.library.queue_manual(record['id'],self.app.pipeline.vocabulary.snapshot(terms))
            self.app.pipeline.wake_event.set(); self.refresh(True)
        terms_dialog(self.window,'用专用词库重新转写',record.get('special',[]),submit,extra)

    def assign(self):
        ids=self.tree.selection()
        if not ids: return
        value=simpledialog.askstring('项目归属','项目名称（留空表示未归类；可多选录音批量设置）：',
            initialvalue=self.rows[ids[0]]['project'],parent=self.window)
        if value is not None: self.library.edit_record(ids,project=value); self.refresh(True)

    def rename(self):
        record=self.selected()
        if record:
            value=simpledialog.askstring('录音名称','便于查找的名称：',initialvalue=record['title'],parent=self.window)
            if value: self.library.edit_record([record['id']],title=value); self.refresh(True)

    def set_current(self):
        record=self.selected(); version=self.selected_version()
        if record and version: self.library.select_version(record['id'],version['id']); self.refresh(True)

    def open_text(self):
        version=self.selected_version()
        if version and Path(version['output']).is_file(): os.startfile(version['output'])

    def open_audio(self):
        record=self.selected()
        if record: os.startfile(str(Path(record['receipt']['path']).parent))

    def correct(self):
        record=self.selected()
        if not record: return
        try: context=self.text.get('sel.first','sel.last')
        except tk.TclError: context=''
        term=simpledialog.askstring('提交新词审核','填写正确词语（只提交词条，不自动修改旧文本）：',parent=self.window)
        if term:
            try:
                self.app.pipeline.vocabulary.add(parse_terms(term),'用户纠正：'+record['title'],'pending',context[:1000])
                self.app.show_vocabulary()
            except ValueError as e: messagebox.showerror('词条格式',str(e),parent=self.window)

    def suggest(self):
        record=self.selected()
        if not record: return
        def submit(terms):
            self.app.pipeline.vocabulary.add(terms,'专用词库：'+record['title'],'pending'); self.app.show_vocabulary()
        terms_dialog(self.window,'挑选适合通用的新词 · 提交后等待审核',record.get('special',[]),submit)

    def cancel(self):
        ids=set(self.tree.selection())
        with self.library.lock:
            for key,job in self.library.data['jobs'].items():
                if job.get('record_id',key) in ids: self.library.cancel(key)
        self.app.pipeline.wake_event.set(); self.refresh(True)

    def import_audio(self):
        path=filedialog.askopenfilename(parent=self.window,filetypes=[('音频','*.wav *.mp3 *.flac *.ogg')])
        if not path: return
        self.status.set('正在复制并校验音频…')
        def work():
            try: self.library.import_audio(path,download_folder(self.root)); self.events.put('音频已复制，自动转写已排队。')
            except Exception as e: self.events.put('导入失败：'+str(e))
        threading.Thread(target=work,daemon=True).start()

    def poll(self):
        if self.closed: return
        self.refresh()
        try: message=self.events.get_nowait()
        except queue.Empty: message=None
        self.status.set(message or self.app.pipeline.asr_status)
        self.window.after(1500,self.poll)

    def close(self):
        self.closed=True; self.app.library_panel=None; self.window.destroy()

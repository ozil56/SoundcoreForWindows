"""Tk vocabulary import, provenance and explicit new-word review."""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from pathlib import Path
from vocabulary import parse_terms


def terms_dialog(parent, title, initial=(), submit=None, extra=None, confirm_label=None):
    window=tk.Toplevel(parent); window.title(title); window.geometry('700x610'); window.minsize(570,490)
    frame=ttk.Frame(window,padding=16); frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='UTF-8 TXT：每行一个词或完整短语；词内空格保留。\n空行和重复词会自动去除，请不要加入编号、标题或说明。').pack(anchor='w')
    if extra: extra(frame)
    text_frame=ttk.Frame(frame); text_frame.pack(fill='both',expand=True,pady=10)
    text=tk.Text(text_frame,height=18,wrap='word',font=('Microsoft YaHei UI',10),undo=True)
    scroll=ttk.Scrollbar(text_frame,orient='vertical',command=text.yview)
    text.configure(yscrollcommand=scroll.set); scroll.pack(side='right',fill='y')
    text.pack(fill='both',expand=True); text.insert('1.0','\n'.join(initial))
    status=tk.StringVar()
    def preview(event=None):
        try:
            terms=parse_terms(text.get('1.0','end')); status.set(f'有效词条：{len(terms)} 个（保存时去重）'); return terms
        except ValueError as e:
            status.set(str(e)); return None
    text.bind('<KeyRelease>',preview); preview()
    def load():
        filename=filedialog.askopenfilename(parent=window,filetypes=[('UTF-8 词库','*.txt')])
        if not filename: return
        try:
            terms=parse_terms(Path(filename).read_text(encoding='utf-8-sig'))
            text.insert('end','\n'+'\n'.join(terms)); preview()
        except (OSError,UnicodeError,ValueError) as e: messagebox.showerror('无法导入',str(e),parent=window)
    def apply():
        terms=preview()
        if terms is None: return
        try:
            if submit: submit(terms)
            window.destroy()
        except Exception as e: messagebox.showerror('未能保存',str(e),parent=window)
    ttk.Label(frame,textvariable=status).pack(anchor='w')
    buttons=ttk.Frame(frame); buttons.pack(fill='x',pady=(10,0))
    ttk.Button(buttons,text='导入 TXT（追加并预览）',command=load).pack(side='left')
    ttk.Button(buttons,text='取消',command=window.destroy).pack(side='right')
    ttk.Button(buttons,text=confirm_label or ('确认并开始' if extra else '确认导入'),command=apply).pack(side='right',padx=8)
    return window


class VocabularyPanel:
    def __init__(self,app,root):
        self.app=app; self.store=app.pipeline.vocabulary; self.closed=False
        self.window=tk.Toplevel(app.root); self.window.title('词库管理 · 通用词库与新词审核')
        self.window.geometry('960x660'); self.window.minsize(780,540)
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        frame=ttk.Frame(self.window,padding=16); frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='通用词库参与每次转写；专用词库在“录音管理”中为单条录音指定。\n待审核词条不会用于识别，确认后才进入通用词库。').pack(anchor='w')
        toolbar=ttk.Frame(frame); toolbar.pack(fill='x',pady=10)
        for label,command in [('导入 / 粘贴词库',self.add),('导出通用 TXT',self.export),('撤销上次操作',self.undo),('查看变更记录',self.audit)]:
            ttk.Button(toolbar,text=label,command=command).pack(side='left',padx=(0,6))
        self.query=tk.StringVar(); self.state=tk.StringVar(value='全部')
        ttk.Entry(toolbar,textvariable=self.query,width=18).pack(side='right'); self.query.trace_add('write',lambda *_:self.refresh())
        ttk.Label(toolbar,text='搜索 ').pack(side='right')
        filterbox=ttk.Combobox(toolbar,textvariable=self.state,values=['全部','已启用','待审核','已停用','已忽略'],state='readonly',width=8)
        filterbox.pack(side='right',padx=6); filterbox.bind('<<ComboboxSelected>>',lambda _:self.refresh())
        self.tree=ttk.Treeview(frame,columns=('term','state','source','context'),show='headings',selectmode='extended')
        for key,label,width in [('term','词条',180),('state','状态',75),('source','来源',230),('context','上下文 / 说明',330)]:
            self.tree.heading(key,text=label); self.tree.column(key,width=width)
        scrollbar=ttk.Scrollbar(frame,orient='vertical',command=self.tree.yview); self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y'); self.tree.pack(fill='both',expand=True)
        self.tree.bind('<Double-1>',lambda _:self.edit())
        self.detail=tk.StringVar()
        ttk.Label(frame,textvariable=self.detail,wraplength=850).pack(anchor='w',pady=(6,0))
        def detail(event=None):
            ids=self.tree.selection()
            row=next((r for r in self.store.rows() if ids and r['id']==ids[0]),None)
            self.detail.set((row.get('source','')+'\n'+row.get('context','')[:500]) if row else '')
        self.tree.bind('<<TreeviewSelect>>',detail)
        bar=ttk.Frame(frame); bar.pack(fill='x',pady=10)
        for label,command in [('通过 / 启用',lambda:self.review('accepted')),('编辑词条',self.edit),('忽略',lambda:self.review('ignored')),('停用',lambda:self.review('disabled'))]:
            ttk.Button(bar,text=label,command=command).pack(side='left',padx=(0,8))
        self.status=tk.StringVar(); ttk.Label(frame,textvariable=self.status).pack(anchor='w'); self.refresh()

    def refresh(self):
        states={'accepted':'已启用','pending':'待审核','disabled':'已停用','ignored':'已忽略'}
        selected=self.tree.selection(); self.tree.delete(*self.tree.get_children())
        rows=self.store.rows()
        for row in rows:
            state=states[row['state']]
            if self.state.get() not in ('全部',state): continue
            if self.query.get().lower() not in (row['term']+' '+row.get('source','')).lower(): continue
            self.tree.insert('', 'end',iid=row['id'],values=(row['term'],state,row.get('source',''),row.get('context','')))
        for item in selected:
            if self.tree.exists(item): self.tree.selection_add(item)
        self.status.set(f"通用词库 {len(self.store.rows('accepted'))} 个词 · 待审核 {len(self.store.rows('pending'))} 个 · 修订 {self.store.data['revision']}")

    def add(self):
        def apply(terms):
            self.store.add(terms,'用户导入 / 粘贴'); self.refresh()
        terms_dialog(self.window,'导入通用词库 · 确认后立即启用',submit=apply)

    def review(self,state):
        ids=self.tree.selection()
        if ids: self.store.review(ids,state); self.refresh()

    def edit(self):
        ids=self.tree.selection()
        if len(ids)!=1: return
        row=next(r for r in self.store.rows() if r['id']==ids[0])
        term=simpledialog.askstring('编辑词条','正确写法：',initialvalue=row['term'],parent=self.window)
        if term is None: return
        try: self.store.review(ids,row['state'],term); self.refresh()
        except ValueError as e: messagebox.showerror('无法编辑',str(e),parent=self.window)

    def export(self):
        path=filedialog.asksaveasfilename(parent=self.window,initialfile='通用词库.txt',defaultextension='.txt')
        if path: self.store.export(path)

    def undo(self):
        self.store.undo(); self.refresh()

    def audit(self):
        window=tk.Toplevel(self.window); window.title('词库变更记录'); window.geometry('660x420')
        text=tk.Text(window,wrap='word'); text.pack(fill='both',expand=True)
        text.insert('1.0','\n'.join(f"修订 {a['revision']} · {a['at']} · {a['action']}" for a in reversed(self.store.data['audit'])))
        text.configure(state='disabled')

    def close(self):
        self.closed=True; self.app.vocabulary_panel=None; self.window.destroy()

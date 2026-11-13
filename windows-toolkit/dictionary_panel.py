from pathlib import Path
import tkinter as tk
from tkinter import ttk,filedialog,messagebox,simpledialog

from ui_common import Panel
from vocabulary import parse_terms
from vocabulary_panel import terms_dialog

STATES={'accepted':'启用','pending':'待审核','disabled':'停用','ignored':'已忽略','deleted':'已删除'}


class TermEditor(ttk.Frame):
    def __init__(self,parent,store,title):
        super().__init__(parent,padding=12)
        self.store,self.title=store,title
        row=ttk.Frame(self);row.pack(fill='x')
        self.search=tk.StringVar();self.filter=tk.StringVar(value='全部')
        ttk.Label(row,text='搜索').pack(side='left')
        ttk.Entry(row,textvariable=self.search,width=34).pack(side='left',padx=8)
        box=ttk.Combobox(row,textvariable=self.filter,values=['全部',*STATES.values()],state='readonly',width=10);box.pack(side='left')
        box.bind('<<ComboboxSelected>>',lambda _:self.refresh());self.search.trace_add('write',lambda *_:self.refresh())
        self.tree=ttk.Treeview(self,columns=('term','state','source'),show='headings',selectmode='extended',height=14)
        for key,label,width in [('term','词条',280),('state','状态',90),('source','来源',360)]:
            self.tree.heading(key,text=label);self.tree.column(key,width=width)
        self.tree.pack(fill='both',expand=True,pady=12);self.tree.bind('<Double-1>',lambda _:self.edit())
        row=ttk.Frame(self);row.pack(fill='x')
        for title,command in [('添加',self.add),('修改',self.edit),('删除',lambda:self.state('deleted')),
                ('停用',lambda:self.state('disabled')),('恢复 / 审核通过',lambda:self.state('accepted')),
                ('导入 TXT',self.import_file),('导出 TXT',self.export)]:
            ttk.Button(row,text=title,command=command).pack(side='left',padx=(0,6))
        self.status=tk.StringVar();ttk.Label(self,textvariable=self.status).pack(anchor='w',pady=10)
        self.refresh()

    def refresh(self):
        selected=self.tree.selection();self.tree.delete(*self.tree.get_children())
        rows=self.store.rows()
        for row in rows:
            if self.filter.get() not in ('全部',STATES[row['state']]):continue
            if self.search.get().lower() not in (row['term']+' '+row.get('source','')).lower():continue
            self.tree.insert('','end',iid=row['id'],values=(row['term'],STATES[row['state']],row.get('source','')))
        for key in selected:
            if self.tree.exists(key):self.tree.selection_add(key)
        self.status.set(f'已启用 {len(self.store.rows("accepted"))} 个 · 待审核 {len(self.store.rows("pending"))} 个。UTF-8 TXT，每行一个词或短语。')

    def add(self):
        def save(terms):self.store.add(terms,'手动添加');self.refresh()
        terms_dialog(self.winfo_toplevel(),'添加词条',submit=save)

    def edit(self):
        keys=self.tree.selection()
        if len(keys)!=1:return
        row=next(r for r in self.store.rows() if r['id']==keys[0])
        value=simpledialog.askstring('修改词条','词条：',initialvalue=row['term'],parent=self.winfo_toplevel())
        if value is None:return
        try:self.store.review(keys,row['state'],value);self.refresh()
        except ValueError as e:messagebox.showerror('无法修改',str(e),parent=self.winfo_toplevel())

    def state(self,state):
        keys=self.tree.selection()
        if not keys:return
        if state=='deleted' and not messagebox.askyesno('删除词条',f'删除选中的 {len(keys)} 个词条？之后可从“已删除”筛选中恢复。',parent=self.winfo_toplevel()):return
        self.store.review(keys,state);self.refresh()

    def import_file(self):
        path=filedialog.askopenfilename(parent=self.winfo_toplevel(),filetypes=[('UTF-8 词库','*.txt')])
        if not path:return
        try:
            terms=parse_terms(Path(path).read_text(encoding='utf-8-sig'))
            def save(items):self.store.add(items,Path(path).name);self.refresh()
            terms_dialog(self.winfo_toplevel(),'预览导入词库',initial=terms,submit=save)
        except Exception as e:messagebox.showerror('无法导入',str(e),parent=self.winfo_toplevel())

    def export(self):
        path=filedialog.asksaveasfilename(parent=self.winfo_toplevel(),initialfile=self.title+'.txt',defaultextension='.txt',filetypes=[('词库','*.txt')])
        if path:self.store.export(path)


class DictionaryPanel(Panel):
    def __init__(self,app,root):
        super().__init__(app,'词库管理','vocabulary_panel',940,640)
        self.projects=app.pipeline.projects
        tabs=ttk.Notebook(self.body);tabs.pack(fill='both',expand=True)
        self.general=TermEditor(tabs,app.pipeline.vocabulary,'通用词库');tabs.add(self.general,text='通用词库')
        self.special=ttk.Frame(tabs,padding=12);tabs.add(self.special,text='专用词库')
        self.project_list=ttk.Frame(self.special);self.project_list.pack(fill='both',expand=True)
        ttk.Label(self.project_list,text='双击项目，编辑该项目的专用词库。停用或删除的项目不会出现在自助转写的可选列表中。').pack(anchor='w',pady=(0,12))
        self.tree=ttk.Treeview(self.project_list,columns=('name','state','count'),show='headings',height=12,selectmode='browse')
        for key,label,width in [('name','项目名称',470),('state','状态',100),('count','启用词条',150)]:
            self.tree.heading(key,text=label);self.tree.column(key,width=width)
        self.tree.pack(fill='both',expand=True);self.tree.bind('<Double-1>',lambda _:self.open_project())
        row=ttk.Frame(self.project_list);row.pack(fill='x',pady=12)
        for label,command in [('添加项目',self.add_project),('修改名称',self.rename),('删除',lambda:self.set_state('deleted')),
                ('停用',lambda:self.set_state('disabled')),('恢复',lambda:self.set_state('accepted')),('编辑词库',self.open_project)]:
            ttk.Button(row,text=label,command=command).pack(side='left',padx=(0,8))
        self.editor_page=None;self.refresh()

    def refresh(self):
        if self.general.store is not self.app.pipeline.vocabulary:
            self.general.store=self.app.pipeline.vocabulary
            self.projects=self.app.pipeline.projects
            if self.editor_page:
                self.editor_page.destroy();self.editor_page=None;self.project_list.pack(fill='both',expand=True)
        self.general.refresh();selected=self.tree.selection();self.tree.delete(*self.tree.get_children())
        for row in self.projects.rows():
            self.tree.insert('','end',iid=row['id'],values=(row['name'],STATES[row['state']],len(self.projects.store(row['id']).rows('accepted'))))
        if selected and self.tree.exists(selected[0]):self.tree.selection_set(selected[0])

    def add_project(self):
        name=simpledialog.askstring('添加项目','项目名：',parent=self.window)
        if not name:return
        try:
            key=self.projects.add(name);self.refresh();self.tree.selection_set(key);self.open_project()
        except Exception as e:self.error(e)

    def rename(self):
        keys=self.tree.selection()
        if not keys:return
        row=next(p for p in self.projects.rows() if p['id']==keys[0])
        name=simpledialog.askstring('修改项目名','项目名：',initialvalue=row['name'],parent=self.window)
        if name is None:return
        try:self.projects.update(keys[0],name=name);self.refresh()
        except Exception as e:self.error(e)

    def set_state(self,state):
        keys=self.tree.selection()
        if not keys:return
        if state=='deleted' and not messagebox.askyesno('删除项目','删除该项目词库？需要时可选中它并点击“恢复”。',parent=self.window):return
        self.projects.update(keys[0],state=state);self.refresh()

    def open_project(self):
        keys=self.tree.selection()
        if not keys:return
        project=next(p for p in self.projects.rows() if p['id']==keys[0])
        self.project_list.pack_forget()
        self.editor_page=ttk.Frame(self.special);self.editor_page.pack(fill='both',expand=True)
        row=ttk.Frame(self.editor_page);row.pack(fill='x')
        ttk.Button(row,text='← 项目列表',command=self.back).pack(side='left')
        ttk.Label(row,text=project['name']+' · '+STATES[project['state']],font=('Microsoft YaHei UI',12,'bold')).pack(side='left',padx=14)
        TermEditor(self.editor_page,self.projects.store(project['id']),project['name']).pack(fill='both',expand=True)

    def back(self):
        self.editor_page.destroy();self.editor_page=None
        self.project_list.pack(fill='both',expand=True);self.refresh()

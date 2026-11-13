import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox


class Panel:
    def __init__(self,app,title,attribute,width=780,height=570):
        self.app,self.attribute=app,attribute
        self.closed=False
        self.window=tk.Toplevel(app.root)
        self.window.title(title)
        self.window.geometry(f'{width}x{height}')
        self.window.minsize(width,height)
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.body=ttk.Frame(self.window,padding=20);self.body.pack(fill='both',expand=True)
        ttk.Label(self.body,text=title,font=('Microsoft YaHei UI',18,'bold')).pack(anchor='w',pady=(0,16))

    def close(self):
        if self.closed:return
        self.closed=True
        setattr(self.app,self.attribute,None)
        self.window.destroy()

    def error(self,error):
        messagebox.showerror('未能完成操作',str(error),parent=self.window)


def open_folder(path):
    path=Path(path);path.mkdir(parents=True,exist_ok=True);os.startfile(path)

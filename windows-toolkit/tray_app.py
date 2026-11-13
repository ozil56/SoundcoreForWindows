"""Seven task-oriented entry points; no general-purpose dashboard or task list."""
import ctypes
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox
import pystray

import soundcore_stream as protocol
from auto_sync import AutoPipeline
from bridge_setup import AutoStart
from recorder_files import load_selection
from device_manager import DeviceManager
from workflow_panels import ConnectionPanel,AutomaticPanel
from settings_panel import SettingsPanel
from self_service_panel import SelfServicePanel
from dictionary_panel import DictionaryPanel
from tool_settings import diagnostics_folder

VERSION='2.5.2'
MENU_ITEMS=(('连接录音豆','devices'),('设备录音管理','files'),('自动转写','sync'),
            ('自助转写','transcribe'),('词库管理','vocabulary'),('设置','settings'),('退出','quit'))


class TrayApp:
    version=VERSION
    def __init__(self,control,start_hidden=False,*,data_root,service_factory,icon_factory):
        self.control=control;self.data_root=Path(data_root);self.icon_factory=icon_factory
        self.root=tk.Tk();self.root.withdraw();self.root.title('录音豆工具箱 v'+VERSION)
        self.root.protocol('WM_DELETE_WINDOW',self.root.withdraw)
        self.commands=queue.Queue();self.closing=False;self.last_report=0;self.last_icon_state=None
        self.tray_ready=threading.Event();self.autostart=AutoStart(self.data_root)
        for name in ('connection_panel','manager','automatic_panel','transcription_panel','vocabulary_panel','settings_panel'):setattr(self,name,None)
        self.log=logging.getLogger('soundcore.bridge');self.log.setLevel(logging.INFO)
        handler=RotatingFileHandler(diagnostics_folder(self.data_root)/'tray-bridge.log',maxBytes=2_000_000,backupCount=3,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'));self.log.addHandler(handler)
        protocol.log=self.message
        self.pipeline=AutoPipeline(self.data_root,self.message)
        self.service=service_factory(self.data_root,self.message,pipeline=self.pipeline)
        if not load_selection(self.data_root):self.service.enabled.clear()
        def action(command):return lambda icon,item:self.commands.put(command)
        menu=pystray.Menu(*(pystray.MenuItem(label,action(command),default=index==0)
            for index,(label,command) in enumerate(MENU_ITEMS)))
        self.tray=pystray.Icon('SoundcoreMicrophone',icon_factory('#94a3b8'),'录音豆工具箱',menu=menu)
        def setup(icon):icon.visible=True;self.tray_ready.set()
        def run():
            try:self.tray.run(setup=setup)
            except Exception:self.log.exception('Tray initialization failed');self.commands.put('devices')
        threading.Thread(target=run,name='SoundcoreTray',daemon=True).start()
        self.service.start();self.pipeline.start();self.root.after(100,self.update)
        if not start_hidden or not load_selection(self.data_root):self.root.after(100,self.show)

    def message(self,text):self.log.info(text)

    def panel(self,attribute,factory):
        if self.closing:return
        panel=getattr(self,attribute)
        if panel and not panel.closed:
            panel.window.deiconify();panel.window.lift()
            if hasattr(panel,'refresh'):panel.refresh()
            return panel
        panel=factory(self,self.data_root);setattr(self,attribute,panel);return panel

    def show(self):return self.panel('connection_panel',ConnectionPanel)
    def show_manager(self):return self.show()
    def show_library(self):return self.panel('manager',lambda app,root:DeviceManager(app,root,'files_only'))
    def show_transcription(self):return self.panel('transcription_panel',SelfServicePanel)
    def show_vocabulary(self):return self.panel('vocabulary_panel',DictionaryPanel)
    def show_sync_panel(self):return self.panel('automatic_panel',AutomaticPanel)
    def show_settings(self):return self.panel('settings_panel',SettingsPanel)

    def sync_device_now(self):
        if not load_selection(self.data_root):self.show();return
        try:
            self.pipeline.request_device_sync()
        except Exception as e:
            panel=self.show_settings();messagebox.showerror('无法开始处理',str(e),parent=panel.window);return
        if self.manager:self.manager.resume_after=True
        elif not self.service.enabled.is_set():self.service.command('resume')
        self.show_sync_panel()

    def dispatch(self,command):
        # Keep old local-control commands working; the tray exposes only seven.
        if command in ('show','devices'):self.show();return
        if command=='files':self.show_library();return
        if command=='transcribe':self.show_transcription();return
        if command=='vocabulary':self.show_vocabulary();return
        if command=='sync':self.show_sync_panel();return
        if command in ('settings','guide'):self.show_settings();return
        if command=='sync-now':self.sync_device_now();return
        if command=='local-now':self.pipeline.request_local_transcription();self.show_sync_panel();return
        if command=='quit':
            self.closing=True;self.quit_deadline=time.monotonic()+50
            for name in ('connection_panel','manager','automatic_panel','transcription_panel','vocabulary_panel','settings_panel'):
                panel=getattr(self,name)
                if panel:panel.close()
            self.pipeline.stop()
        self.service.command(command)

    def update(self):
        for command in self.control.pending():self.dispatch(command)
        while not self.commands.empty():
            try:self.dispatch(self.commands.get_nowait())
            except Exception as error:self.message('操作未完成：'+str(error))
        snapshot=self.service.snapshot();snapshot['automation']=self.pipeline.snapshot()
        if self.manager:snapshot['status']='设备录音管理正在使用连接'
        elif not load_selection(self.data_root):snapshot['status']='请先选择录音豆'
        state=(snapshot['status'],snapshot['color'])
        if self.tray_ready.is_set() and state!=self.last_icon_state:
            self.tray.icon=self.icon_factory(snapshot['color']);self.tray.title='录音豆 · '+snapshot['status'];self.last_icon_state=state
        if time.monotonic()-self.last_report>1:
            self.last_report=time.monotonic()
            snapshot.update(tray_ready=self.tray_ready.is_set(),window_visible=any(getattr(self,name) is not None for name in
                ('connection_panel','manager','automatic_panel','transcription_panel','vocabulary_panel','settings_panel')),
                console_window=bool(ctypes.windll.kernel32.GetConsoleWindow()),updated_at=dt.datetime.now().astimezone().isoformat())
            try:
                target=diagnostics_folder(self.data_root)/'bridge-status.json';temporary=target.with_suffix('.tmp')
                temporary.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temporary,target)
            except OSError as e:self.log.warning('Status: %s',e)
        if self.closing and ((not snapshot['running'] and self.manager is None and not snapshot['automation']['worker_running']) or time.monotonic()>self.quit_deadline):
            self.tray.stop();self.control.close();self.root.destroy();return
        self.root.after(150,self.update)

"""Local Windows microphone bridge. Audio stays in memory, forwarded to VB-CABLE."""
import argparse
import asyncio
import ctypes
from ctypes import wintypes
import datetime as dt
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import numpy as np
import sounddevice as sd
import pystray
from PIL import Image, ImageDraw
import soundcore_stream as protocol
from bridge_setup import AutoStart, environment_summary, GUIDE
from recorder_files import load_selection
from device_manager import DeviceManager
from auto_sync import AutoPipeline
from sync_panel import SyncPanel
from local_library_panel import LocalLibraryPanel
from local_transcription_panel import LocalTranscriptionPanel
from vocabulary_panel import VocabularyPanel
from tool_settings import diagnostics_folder

ROOT = Path(__file__).resolve().parent
# The host Python installation has Tcl in tcl/, outside Tk's fallback search.
for variable, subfolder in [('TCL_LIBRARY','tcl8.6'),('TK_LIBRARY','tk8.6')]:
    library = Path(sys.base_prefix)/'tcl'/subfolder
    if library.is_dir():
        os.environ.setdefault(variable,str(library))


def is_remote_session():
    """Query the live session, including RDP -> physical-console transitions."""
    api=ctypes.WinDLL('wtsapi32',use_last_error=True)
    api.WTSQuerySessionInformationW.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(wintypes.DWORD)]
    api.WTSQuerySessionInformationW.restype=wintypes.BOOL
    api.WTSFreeMemory.argtypes=[ctypes.c_void_p]
    api.WTSFreeMemory.restype=None
    buffer=ctypes.c_void_p(); size=wintypes.DWORD()
    try:
        # WTS_CURRENT_SESSION / WTSClientProtocolType: 0 = console, 2 = RDP.
        if api.WTSQuerySessionInformationW(None,0xffffffff,16,ctypes.byref(buffer),ctypes.byref(size)):
            if buffer.value and size.value>=ctypes.sizeof(ctypes.c_ushort):
                return ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ushort)).contents.value != 0
        # A PC without Remote Desktop Services may not expose WTS information.
        return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))
    finally:
        if buffer.value: api.WTSFreeMemory(buffer)


class BluetoothThreadCOM:
    """Keep the BLE asyncio thread MTA when PortAudio refreshes WASAPI devices."""
    def __enter__(self):
        self.api=ctypes.WinDLL('ole32')
        self.api.CoInitializeEx.argtypes=[ctypes.c_void_p,wintypes.DWORD]
        self.api.CoInitializeEx.restype=ctypes.c_long
        self.api.CoUninitialize.argtypes=[]
        self.api.CoUninitialize.restype=None
        result=self.api.CoInitializeEx(None,0)  # COINIT_MULTITHREADED
        if result<0: raise OSError(f'蓝牙后台线程初始化失败：0x{result & 0xffffffff:08X}')
        return self

    def __exit__(self,*_):
        self.api.CoUninitialize()


class CableOutput:
    def __init__(self):
        candidates = [i for i,d in enumerate(sd.query_devices())
                      if d['name'].startswith('CABLE Input') and d['max_output_channels']>=2
                      and sd.query_hostapis(d['hostapi'])['name']=='Windows WASAPI']
        self.backend='Windows WASAPI'
        if len(candidates)!=1:
            raise RuntimeError('未找到 CABLE Input。请确认 VB-CABLE 已安装，必要时重启电脑。')
        self.device = candidates[0]
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.primed = False
        self.underruns = 0
        self.dropped_bytes = 0
        self.stream = sd.RawOutputStream(device=self.device,samplerate=16000,channels=2,
            dtype='int16',blocksize=320,latency=.08,
            extra_settings=sd.WasapiSettings(auto_convert=True),
            callback=self.callback)

    def callback(self,outdata,frames,time_info,status):
        needed = frames*4
        result = bytearray(needed)
        with self.lock:
            if not self.primed and len(self.buffer)>=19200:
                self.primed=True
            if self.primed:
                take=min(needed,len(self.buffer))
                result[:take]=self.buffer[:take]
                del self.buffer[:take]
                if take<needed:
                    self.underruns+=1
                    self.primed=False
        outdata[:]=result

    def push(self,pcm):
        stereo=np.repeat(np.frombuffer(pcm,dtype='<i2'),2).tobytes()
        with self.lock:
            self.buffer.extend(stereo)
            # Keep latency bounded if BLE briefly delivers a backlog.
            if len(self.buffer)>64000:
                trim=len(self.buffer)-25600
                trim-=trim%4
                del self.buffer[:trim]
                self.dropped_bytes+=trim

    def __enter__(self):
        try:
            self.stream.start()
        except BaseException:
            self.stream.close()
            raise
        return self

    def __exit__(self,*_):
        try:
            self.stream.stop()
        finally:
            self.stream.close()


class OptionalCableOutput:
    """Console-only WASAPI output; recording backups remain independent."""
    def __init__(self):
        self.output=None
        self.error=''
        self.retry_at=0
        self.remote_session=False

    @property
    def underruns(self): return self.output.underruns if self.output else 0

    @property
    def dropped_bytes(self): return self.output.dropped_bytes if self.output else 0

    def close(self):
        output,self.output=self.output,None
        if output:
            try: output.__exit__()
            except Exception as error: self.error=str(error)

    def poll(self):
        if is_remote_session():
            self.remote_session=True
            self.close()
            self.error=''
            self.retry_at=0
            return
        if self.remote_session:
            self.remote_session=False
            self.retry_at=0
        if self.output:
            try:
                if self.output.stream.active: return
            except Exception as error: self.error=str(error)
            self.close()
            self.retry_at=0
        if time.monotonic()<self.retry_at: return
        self.retry_at=time.monotonic()+5
        try:
            # No stream is open here. Drop cached RDP endpoints before opening
            # the same CABLE Input on the physical console; never choose KS.
            sd._terminate()
            sd._initialize()
            output=CableOutput()
            output.__enter__()
            self.output=output
            self.error=''
        except Exception as error:
            self.error=str(error)

    def push(self,pcm):
        if self.output: self.output.push(pcm)

    def __enter__(self):
        self.poll()
        return self

    def __exit__(self,*args):
        self.close()


def icon_image(color='#3b82f6'):
    """Simple microphone silhouette, legible at Windows tray size."""
    image=Image.new('RGBA',(64,64),(0,0,0,0))
    draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((3,3,61,61),radius=15,fill='#15283e')
    draw.rounded_rectangle((23,10,39,37),radius=8,fill='white')
    draw.arc((16,23,46,47),0,180,fill='white',width=4)
    draw.line((31,46,31,53),fill='white',width=4)
    draw.line((23,53,39,53),fill='white',width=4)
    draw.ellipse((44,44,62,62),fill=color,outline='#15283e',width=2)
    return image


class InstanceControl:
    """Per-user/session singleton and local control events; no network listener."""
    def __init__(self,root):
        suffix=hashlib.sha256(str(root.resolve()).lower().encode()).hexdigest()[:20]
        self.prefix='Local\\SoundcoreMicrophone.'+suffix
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
        self.api.CreateMutexW.restype=wintypes.HANDLE
        self.api.CreateEventW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.BOOL,wintypes.LPCWSTR]
        self.api.CreateEventW.restype=wintypes.HANDLE
        self.api.OpenEventW.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR]
        self.api.OpenEventW.restype=wintypes.HANDLE
        self.api.SetEvent.argtypes=[wintypes.HANDLE]
        self.api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        self.api.CloseHandle.argtypes=[wintypes.HANDLE]
        self.mutex=None
        self.events={}

    def acquire(self):
        self.mutex=self.api.CreateMutexW(None,False,self.prefix)
        if not self.mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        already=ctypes.get_last_error()==183
        if already:
            self.api.CloseHandle(self.mutex); self.mutex=None
            return False
        for name in ('show','pause','resume','reconnect','devices','files','transcribe','sync','sync-now','vocabulary','quit'):
            handle=self.api.CreateEventW(None,False,False,self.prefix+'.'+name)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self.events[name]=handle
        return True

    def send(self,name):
        handle=self.api.OpenEventW(2,False,self.prefix+'.'+name)
        if not handle:
            return False
        try: return bool(self.api.SetEvent(handle))
        finally: self.api.CloseHandle(handle)

    def pending(self):
        return [name for name,handle in self.events.items() if self.api.WaitForSingleObject(handle,0)==0]

    def close(self):
        for handle in self.events.values(): self.api.CloseHandle(handle)
        self.events.clear()
        if self.mutex: self.api.CloseHandle(self.mutex); self.mutex=None


class BridgeService:
    """Reconnect supervisor, running BLE on one worker event loop."""
    def __init__(self,root,message,receiver_factory=protocol.Receiver,output_factory=OptionalCableOutput,retry_seconds=5,pipeline=None):
        self.root=root
        self.message=message
        self.receiver_factory=receiver_factory
        self.output_factory=output_factory
        self.retry_seconds=retry_seconds
        self.enabled=threading.Event(); self.enabled.set()
        self.idle=threading.Event()
        self.shutdown=threading.Event()
        self.receiver=None
        self.output=None
        self.loop=None
        self.wakeup=None
        self.task=None
        self.worker=None
        self.attempts=0
        self.last_error=''
        self.last_error_kind=''
        self.total_frames=0
        self.total_samples=0
        self.pipeline=pipeline

    def start(self):
        self.worker=threading.Thread(target=self._work,name='SoundcoreBLE',daemon=True)
        self.worker.start()

    def _work(self):
        try:
            with BluetoothThreadCOM(): asyncio.run(self.run())
        except Exception as error:
            self.last_error=str(error)
            self.message(f'后台连接服务异常：{type(error).__name__}: {error}')

    def command(self,command):
        if command=='pause':
            self.enabled.clear()
            self.idle.clear()
        if command in ('resume','reconnect'): self.enabled.set()
        if command=='quit': self.shutdown.set()
        def wake():
            if self.wakeup: self.wakeup.set()
            if self.task and not self.task.done(): self.task.cancel()
        if self.loop and not self.loop.is_closed():
            try: self.loop.call_soon_threadsafe(wake)
            except RuntimeError: pass

    async def attempt(self):
        folder=diagnostics_folder(self.root)/'tray-runtime'
        folder.mkdir(parents=True,exist_ok=True)
        receiver=None
        monitor=None
        try:
            with self.output_factory() as output:
                self.output=output
                receiver=self.receiver_factory(folder,pcm_callback=output.push,save_audio=False)
                self.receiver=receiver
                if self.pipeline:
                    receiver.idle_callback=self.pipeline.when_idle
                if hasattr(output,'poll'):
                    async def watch_audio():
                        while True:
                            await asyncio.sleep(1)
                            output.poll()
                    monitor=asyncio.create_task(watch_audio())
                try:
                    await receiver.run(seconds=None,wait_for_recording=True)
                finally:
                    if monitor:
                        monitor.cancel()
                        try: await monitor
                        except asyncio.CancelledError: pass
        finally:
            if receiver:
                self.total_frames+=receiver.frames_ok
                self.total_samples+=receiver.samples
                receiver.close()
            self.receiver=None
            self.output=None

    async def run(self):
        self.loop=asyncio.get_running_loop()
        self.wakeup=asyncio.Event()
        while not self.shutdown.is_set():
            if not self.enabled.is_set():
                self.idle.set()
                await self.wakeup.wait(); self.wakeup.clear()
                continue
            self.idle.clear()
            self.attempts+=1
            self.last_error=''
            self.last_error_kind=''
            self.task=asyncio.create_task(self.attempt())
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                self.last_error=f'{type(error).__name__}: {error}'
                self.last_error_kind='cable' if 'VB-CABLE' in str(error) else 'connection'
                self.message(f'等待自动重连：{self.last_error}')
            finally:
                self.task=None
                self.idle.set()
            if not self.shutdown.is_set() and self.enabled.is_set():
                try: await asyncio.wait_for(self.wakeup.wait(),self.retry_seconds)
                except asyncio.TimeoutError: pass
                self.wakeup.clear()

    def snapshot(self):
        receiver=self.receiver
        output=self.output
        fresh=bool(receiver and receiver.last_audio and time.monotonic()-receiver.last_audio<3)
        phase=receiver.phase if receiver else 'waiting_device'
        if self.shutdown.is_set(): status,color='正在退出…','#94a3b8'
        elif not self.enabled.is_set(): status,color='已暂停自动连接','#94a3b8'
        elif self.last_error_kind=='cable': status,color='请先安装 VB-CABLE 虚拟声卡并重启','#f59e0b'
        elif fresh and phase=='streaming' and getattr(output,'remote_session',False): status,color='远程会话：麦克风桥接暂停，设备仍在录音','#94a3b8'
        elif fresh and phase=='streaming' and getattr(output,'error',''): status,color='正在收音，虚拟声卡暂不可用','#f59e0b'
        elif fresh and phase=='streaming': status,color='正在收音 → Typeless','#22c55e'
        elif phase=='syncing': status,color='设备空闲，正在自动下载录音','#3b82f6'
        elif phase=='waiting' and getattr(output,'remote_session',False): status,color='已连接；远程会话不启用麦克风桥接','#3b82f6'
        elif phase=='waiting': status,color='已连接，等待录音豆开始录音','#3b82f6'
        elif phase in ('connecting','starting'): status,color='正在连接录音豆…','#f59e0b'
        else: status,color='等待录音豆开启，自动连接中','#94a3b8'
        return {'version':'2.4.3','pid':os.getpid(),'running':bool(self.worker and self.worker.is_alive()),
                'auto_connect':self.enabled.is_set(),'status':status,'phase':phase,'color':color,
                'attempts':self.attempts,'last_error':self.last_error,
                'audio_output_error':getattr(output,'error',''),
                'audio_suspended_remote':getattr(output,'remote_session',False),
                'audio_backend':getattr(getattr(output,'output',output),'backend',''),
                'frames':self.total_frames+(receiver.frames_ok if receiver else 0),
                'seconds':(self.total_samples+(receiver.samples if receiver else 0))/16000,
                'peak':receiver.last_peak if fresh else 0,
                'decode_errors':receiver.frames_failed if receiver else 0,
                'missing_frames':receiver.gaps if receiver else 0,
                'output_underruns':output.underruns if output else 0,
                'output_dropped_bytes':output.dropped_bytes if output else 0}


class App:
    def __init__(self,control,start_hidden=False):
        self.control=control
        self.root=tk.Tk()
        self.root.withdraw()
        self.root.title('录音豆工具箱 v2.4.3 · 本地音频转写')
        self.root.geometry('740x670')
        self.root.minsize(700,640)
        self.events=queue.Queue()
        self.commands=queue.Queue()
        self.closing=False
        self.last_report=0
        self.last_icon_state=None
        self.tray_ready=threading.Event()
        self.autostart=AutoStart(ROOT)
        self.autostart_var=tk.BooleanVar(value=self.autostart.enabled())
        self.guide_window=None
        self.manager=None
        self.sync_panel=None
        self.library_panel=None
        self.transcription_panel=None
        self.vocabulary_panel=None
        self.status=tk.StringVar(value='后台启动中…')
        self.stats=tk.StringVar(value='等待录音豆')
        self.level=tk.DoubleVar(value=0)
        self.log=logging.getLogger('soundcore.bridge')
        self.log.setLevel(logging.INFO)
        handler=RotatingFileHandler(diagnostics_folder(ROOT)/'tray-bridge.log',maxBytes=2_000_000,backupCount=3,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        self.log.addHandler(handler)
        protocol.log=self.message
        self.pipeline=AutoPipeline(ROOT,self.message)
        self.service=BridgeService(ROOT,self.message,pipeline=self.pipeline)
        if not load_selection(ROOT): self.service.enabled.clear()
        outer=ttk.Frame(self.root,padding=22); outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='录音豆 → Typeless',font=('Microsoft YaHei UI',20,'bold')).pack(anchor='w')
        ttk.Label(outer,textvariable=self.status,font=('Microsoft YaHei UI',12)).pack(anchor='w',pady=(12,8))
        ttk.Progressbar(outer,variable=self.level,maximum=100).pack(fill='x')
        ttk.Label(outer,textvariable=self.stats).pack(anchor='w',pady=(8,12))
        buttons=ttk.Frame(outer); buttons.pack(fill='x')
        self.toggle_button=ttk.Button(buttons,text='暂停自动连接',command=lambda:self.dispatch('toggle'))
        self.toggle_button.pack(side='left')
        ttk.Button(buttons,text='立即重连',command=lambda:self.dispatch('reconnect')).pack(side='left',padx=10)
        ttk.Button(buttons,text='收起到托盘',command=self.root.withdraw).pack(side='left')
        ttk.Button(buttons,text='选择录音豆',command=self.show_manager).pack(side='right')
        tools=ttk.Frame(outer); tools.pack(fill='x',pady=(10,0))
        ttk.Button(tools,text='转写本地音频',command=self.show_transcription).pack(side='left')
        ttk.Button(tools,text='录音管理',command=self.show_library).pack(side='left',padx=8)
        ttk.Button(tools,text='词库管理',command=self.show_vocabulary).pack(side='left')
        ttk.Button(tools,text='日常录音自动处理',command=self.show_sync_panel).pack(side='right')
        settings=ttk.Frame(outer); settings.pack(fill='x',pady=(12,0))
        ttk.Checkbutton(settings,text='登录 Windows 后自动启动',variable=self.autostart_var,
                        command=self.change_autostart).pack(side='left')
        ttk.Button(settings,text='首次使用 / 检查环境',command=self.show_guide).pack(side='right')
        ttk.Label(outer,text='开启录音豆后会自动连接，断线后自动重连。\n'
                  'Typeless 麦克风保持选择 CABLE Output，用右 Alt 开始、结束听写。\n'
                  '关闭此窗口后仍在托盘运行；彻底退出请右键托盘图标。\n'
                  '用完弹起录音豆侧键，停止设备自身的录音。',
                  wraplength=560,justify='left').pack(anchor='w',pady=16)
        ttk.Label(outer,text='自动同步、校验后删除与本地转写可在设置面板中启用并设置运行时间。\n绿色：收音中；蓝色：已连接或下载中；灰色：等待或暂停。',
                  foreground='#666666',wraplength=560).pack(anchor='w')
        self.messages=tk.Text(outer,height=8,font=('Consolas',9),wrap='word',state='disabled')
        self.messages.pack(fill='both',expand=True,pady=(12,0))
        self.root.protocol('WM_DELETE_WINDOW',self.root.withdraw)
        self.tray=pystray.Icon('SoundcoreMicrophone',icon_image('#94a3b8'),'录音豆麦克风 · 等待连接',
            menu=pystray.Menu(
                pystray.MenuItem('显示状态',lambda icon,item:self.commands.put('show'),default=True),
                pystray.MenuItem(lambda item:'暂停自动连接' if self.service.enabled.is_set() else '恢复自动连接',
                                 lambda icon,item:self.commands.put('toggle')),
                pystray.MenuItem('立即重连',lambda icon,item:self.commands.put('reconnect')),
                pystray.MenuItem('转写本地音频',lambda icon,item:self.commands.put('transcribe')),
                pystray.MenuItem('立即下载并转写设备录音',lambda icon,item:self.commands.put('sync-now')),
                pystray.MenuItem('录音管理',lambda icon,item:self.commands.put('files')),
                pystray.MenuItem('词库管理 / 新词审核',lambda icon,item:self.commands.put('vocabulary')),
                pystray.MenuItem('日常录音自动处理',lambda icon,item:self.commands.put('sync')),
                pystray.MenuItem('选择录音豆',lambda icon,item:self.commands.put('devices')),
                pystray.MenuItem('首次使用 / 检查环境',lambda icon,item:self.commands.put('guide')),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('退出',lambda icon,item:self.commands.put('quit'))))
        def tray_setup(icon):
            icon.visible=True
            self.tray_ready.set()
        def tray_run():
            try: self.tray.run(setup=tray_setup)
            except Exception:
                self.log.exception('Tray initialization failed')
                self.commands.put('show')
        self.tray_thread=threading.Thread(target=tray_run,name='SoundcoreTray',daemon=True)
        self.tray_thread.start()
        self.service.start()
        self.pipeline.start()
        self.root.after(100,self.update)
        if not start_hidden or not load_selection(ROOT): self.root.after(100,self.show)

    def message(self,text):
        self.log.info(text)
        self.events.put(f'{dt.datetime.now():%H:%M:%S} {text}')

    def show(self):
        self.root.deiconify(); self.root.lift()

    def show_sync_panel(self):
        if self.closing: return
        if self.sync_panel and not self.sync_panel.closed:
            self.sync_panel.window.lift(); return
        self.sync_panel=SyncPanel(self,ROOT)

    def sync_device_now(self):
        if self.closing: return
        if not load_selection(ROOT):
            self.show_manager()
            self.message('请先选择录音豆，再点击“立即下载并转写设备录音”。')
            return
        try:
            self.pipeline.request_device_sync()
        except (OSError, ValueError, RuntimeError) as error:
            self.show_sync_panel()
            messagebox.showerror('无法立即处理设备录音',str(error),parent=self.sync_panel.window)
            return
        if self.manager:
            self.manager.resume_after=True
            self.message('立即处理已排队，请关闭“设备与录音”窗口，释放设备连接。')
        elif not self.service.enabled.is_set():
            self.service.command('resume')
        self.show_sync_panel()

    def show_library(self):
        if self.closing: return
        if self.library_panel and not self.library_panel.closed:
            self.library_panel.window.lift(); return
        self.library_panel=LocalLibraryPanel(self,ROOT)

    def show_transcription(self):
        if self.closing: return
        if self.transcription_panel and not self.transcription_panel.closed:
            self.transcription_panel.window.deiconify(); self.transcription_panel.window.lift(); return
        self.transcription_panel=LocalTranscriptionPanel(self,ROOT)

    def show_vocabulary(self):
        if self.closing: return
        if self.vocabulary_panel and not self.vocabulary_panel.closed:
            self.vocabulary_panel.refresh(); self.vocabulary_panel.window.lift(); return
        self.vocabulary_panel=VocabularyPanel(self,ROOT)

    def show_manager(self,tab='devices'):
        if self.closing: return
        if self.manager and not self.manager.closed:
            self.manager.tabs.select(1 if tab=='files' else 0)
            self.manager.window.lift(); return
        self.manager=DeviceManager(self,ROOT,tab)

    def change_autostart(self):
        try:
            self.autostart.set_enabled(self.autostart_var.get())
            self.message('已启用登录后自动启动' if self.autostart_var.get() else '已关闭本工具包的登录自启动')
        except (OSError,ValueError) as error:
            self.autostart_var.set(self.autostart.enabled())
            messagebox.showerror('无法设置自动启动',str(error),parent=self.root)

    def show_guide(self):
        if self.guide_window and self.guide_window.winfo_exists():
            self.guide_window.lift(); return
        self.show()
        self.guide_window=tk.Toplevel(self.root)
        self.guide_window.title('首次使用 · 录音豆麦克风工具包')
        self.guide_window.geometry('740x660')
        frame=ttk.Frame(self.guide_window,padding=16); frame.pack(fill='both',expand=True)
        ttk.Label(frame,text=environment_summary(ROOT),wraplength=700,justify='left').pack(anchor='w',pady=(0,12))
        text=tk.Text(frame,wrap='word',font=('Microsoft YaHei UI',10),padx=12,pady=12)
        scroll=ttk.Scrollbar(frame,command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y'); text.pack(fill='both',expand=True)
        text.insert('1.0',GUIDE); text.configure(state='disabled')

    def dispatch(self,command):
        if command=='show': self.show(); return
        if command=='guide': self.show_guide(); return
        if command=='devices': self.show_manager(); return
        if command=='files': self.show_library(); return
        if command=='transcribe': self.show_transcription(); return
        if command=='vocabulary': self.show_vocabulary(); return
        if command=='sync': self.show_sync_panel(); return
        if command=='sync-now': self.sync_device_now(); return
        if command=='toggle': command='pause' if self.service.enabled.is_set() else 'resume'
        if command in ('resume','reconnect'):
            if self.manager:
                self.manager.resume_after=True
                self.manager.window.lift()
                self.message('请先关闭“设备与录音”窗口，随后恢复麦克风。')
                return
            if not load_selection(ROOT):
                self.show_manager()
                self.manager.resume_after=True
                return
        if command=='quit':
            self.closing=True
            self.quit_deadline=time.monotonic()+50
            self.root.withdraw()
            if self.manager: self.manager.close()
            if self.sync_panel: self.sync_panel.close()
            if self.library_panel: self.library_panel.close()
            if self.transcription_panel: self.transcription_panel.close()
            if self.vocabulary_panel: self.vocabulary_panel.close()
            self.pipeline.stop()
        self.service.command(command)
        self.tray.update_menu()

    def update(self):
        for command in self.control.pending(): self.dispatch(command)
        while True:
            try: self.dispatch(self.commands.get_nowait())
            except queue.Empty: break
        while True:
            try: message=self.events.get_nowait()
            except queue.Empty: break
            self.messages.configure(state='normal')
            self.messages.insert('end',message+'\n')
            self.messages.see('end')
            if int(self.messages.index('end-1c').split('.')[0])>180:
                self.messages.delete('1.0','50.0')
            self.messages.configure(state='disabled')
        snapshot=self.service.snapshot()
        snapshot['automation']=self.pipeline.snapshot()
        if self.manager:
            snapshot['status']='设备与录音窗口已打开，麦克风暂时暂停'
            snapshot['recording_manager']={'busy':self.manager.busy,
                'recording_count':len(self.manager.recordings),
                'candidate_count':len(self.manager.candidates),
                'remembered_device_visible':bool(load_selection(ROOT) and
                    self.manager.devices.exists(load_selection(ROOT)['identity_hex'])),
                'resume_after_close':self.manager.resume_after,
                'status':self.manager.status.get(),
                'download_dir':str(self.manager.output_folder)}
        elif not load_selection(ROOT):
            snapshot['status']='首次使用：点击“选择录音豆”选择自己的设备'
        self.status.set(snapshot['status'])
        self.level.set(min(100,snapshot['peak']*200))
        self.stats.set(f"累计收音 {snapshot['seconds']:.1f} 秒   本次错误 {snapshot['decode_errors']}   丢帧 {snapshot['missing_frames']}")
        self.toggle_button.configure(text='暂停自动连接' if snapshot['auto_connect'] else '恢复自动连接')
        icon_state=(snapshot['status'],snapshot['color'])
        if self.tray_ready.is_set() and icon_state!=self.last_icon_state:
            self.tray.icon=icon_image(snapshot['color'])
            self.tray.title='录音豆麦克风 · '+snapshot['status']
            self.last_icon_state=icon_state
        if time.monotonic()-self.last_report>1:
            self.last_report=time.monotonic()
            snapshot.update(tray_ready=self.tray_ready.is_set(),window_visible=self.root.state()!='withdrawn',
                            console_window=bool(ctypes.windll.kernel32.GetConsoleWindow()),
                            updated_at=dt.datetime.now().astimezone().isoformat())
            try:
                target=diagnostics_folder(ROOT)/'bridge-status.json'
                temporary=target.with_suffix('.tmp')
                temporary.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
                os.replace(temporary,target)
            except OSError as error: self.log.warning('Status write: %s',error)
        if self.closing and ((not snapshot['running'] and self.manager is None and not snapshot['automation']['worker_running']) or time.monotonic()>self.quit_deadline):
            self.tray.stop()
            self.control.close()
            self.root.destroy()
            return
        self.root.after(150,self.update)


def main():
    global ROOT
    parser=argparse.ArgumentParser()
    parser.add_argument('--connect',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--tray',action='store_true')
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--command',choices=['show','pause','resume','reconnect','devices','files','transcribe','sync','sync-now','vocabulary','quit'])
    args=parser.parse_args()
    if args.data_dir: ROOT=args.data_dir.resolve()
    elif getattr(sys,'frozen',False):
        ROOT=Path(sys.executable).resolve().parent
        organized=ROOT.parent/'数据'
        if (organized/'tool-settings.json').is_file(): ROOT=organized
    protocol.ROOT=ROOT
    diagnostics_folder(ROOT).mkdir(parents=True,exist_ok=True)
    control=InstanceControl(ROOT)
    if args.command:
        return 0 if control.send(args.command) else 1
    if not control.acquire():
        if not args.tray: control.send('show')
        return 0
    try:
        App(control,start_hidden=args.tray).root.mainloop()
    finally:
        control.close()
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception:
        import traceback
        diagnostics_folder(ROOT).mkdir(parents=True,exist_ok=True)
        (diagnostics_folder(ROOT)/'startup-error.log').write_text(traceback.format_exc(),encoding='utf-8')
        raise

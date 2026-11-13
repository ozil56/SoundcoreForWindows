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
        for name in ('show','pause','resume','reconnect','devices','files','transcribe','sync','sync-now','local-now','settings','disconnect','vocabulary','quit'):
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
        self.reconnect_at=0

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
        if command=='disconnect':self.reconnect_at=time.monotonic()+30
        if command=='pause':
            self.enabled.clear()
            self.idle.clear()
        if command in ('resume','reconnect'):
            self.reconnect_at=0
            self.enabled.set()
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
            if self.enabled.is_set() and time.monotonic()<self.reconnect_at:
                self.idle.set()
                try:await asyncio.wait_for(self.wakeup.wait(),self.reconnect_at-time.monotonic())
                except asyncio.TimeoutError:pass
                self.wakeup.clear()
                continue
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
        elif time.monotonic()<self.reconnect_at:status,color=f'已断开，{max(1,int(self.reconnect_at-time.monotonic()))} 秒后自动重连','#94a3b8'
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
        return {'version':'2.5.0','pid':os.getpid(),'running':bool(self.worker and self.worker.is_alive()),
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


from tray_app import TrayApp as App


def main():
    global ROOT
    parser=argparse.ArgumentParser()
    parser.add_argument('--connect',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--tray',action='store_true')
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--command',choices=['show','pause','resume','reconnect','devices','files','transcribe','sync','sync-now','local-now','settings','disconnect','vocabulary','quit'])
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
        App(control,start_hidden=args.tray,data_root=ROOT,service_factory=BridgeService,icon_factory=icon_image).root.mainloop()
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

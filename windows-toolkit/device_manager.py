"""Device picker and local WAV export UI; pauses microphone while open."""
import asyncio
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from recorder_files import (Candidate, RecorderSession, load_selection, save_selection, scan,
                            download_folder, set_download_folder, download_one)


class DeviceManager:
    def __init__(self, app, data_root, tab='devices'):
        self.app, self.data_root = app, data_root
        remembered = load_selection(data_root)
        phase = getattr(getattr(app.service, 'receiver', None), 'phase', '')
        self.connected_identity = (remembered['identity_hex'] if remembered and phase in ('waiting','streaming') else None)
        self.resume_after = app.service.enabled.is_set() or not load_selection(data_root)
        app.service.command('pause')
        self.window = tk.Toplevel(app.root)
        self.window.title('录音管理 · soundcore Work')
        self.window.geometry('900x720')
        self.window.minsize(840, 680)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.events = queue.Queue()
        self.busy = self.closing = self.closed = False
        self.loop = self.task = None
        self.cancel_requested = threading.Event()
        self.deleting = False
        self.candidates, self.recordings = {}, {}
        self.scan_results = []
        self.has_scanned = False
        self.output_folder = download_folder(data_root)
        self.folder_label = tk.StringVar(value=str(self.output_folder))
        self.delete_after = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='麦克风连接已暂时暂停；关闭本窗口后恢复原状态。')
        self.selected = tk.StringVar()
        self.progress = tk.DoubleVar(value=0)
        outer = ttk.Frame(self.window, padding=18); outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='选择录音豆与导出录音', font=('Microsoft YaHei UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(outer, textvariable=self.selected).pack(anchor='w', pady=(8, 12))
        tabs = self.tabs = ttk.Notebook(outer); tabs.pack(fill='both', expand=True)
        devices = ttk.Frame(tabs, padding=12); tabs.add(devices, text='选择录音豆')
        files = ttk.Frame(tabs, padding=12); tabs.add(files, text='设备内录音')
        ttk.Label(devices, text='取出并唤醒录音豆；让手机 App 和其他电脑释放蓝牙连接。\n'
                  '只选择你要使用的 soundcore Work / D3200。名称相同时可核对设备标识。',
                  wraplength=720).pack(anchor='w', pady=(0, 10))
        self.devices = ttk.Treeview(devices, columns=('name', 'identity', 'rssi', 'state'), show='headings', height=9, selectmode='browse')
        for column, title, width in [('name','设备名称',190),('identity','设备标识',170),('rssi','信号 dBm',85),('state','选择 / 发现状态',275)]:
            self.devices.heading(column, text=title); self.devices.column(column, width=width)
        self.devices.pack(fill='both', expand=True)
        actions = ttk.Frame(devices); actions.pack(fill='x', pady=(12, 0))
        self.scan_button = ttk.Button(actions, text='扫描附近的录音豆', command=self.scan_devices); self.scan_button.pack(side='left')
        self.select_button = ttk.Button(actions, text='记住所选录音豆', command=self.select_device); self.select_button.pack(side='left', padx=10)
        ttk.Label(files, text='可按 Ctrl / Shift 多选；下载为 WAV，可直接用 Windows 播放。下载历史录音前，请先停止设备录音。\n'
                  '管理期间麦克风桥接暂停；未勾选“下载后删除”时保留设备原件。长录音传输可能需要较长时间。',
                  wraplength=720).pack(anchor='w', pady=(0, 10))
        self.files = ttk.Treeview(files, columns=('time','id','size','state'), show='headings', height=9, selectmode='extended')
        for column, title, width in [('time','开始时间（设备时钟）',190),('id','文件编号',135),('size','设备文件大小',125),('state','状态',145)]:
            self.files.heading(column, text=title); self.files.column(column, width=width)
        scroll = ttk.Scrollbar(files, orient='vertical', command=self.files.yview)
        self.files.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.files.pack(fill='both', expand=True)
        actions = ttk.Frame(files); actions.pack(fill='x', pady=(12, 0))
        self.list_button = ttk.Button(actions, text='读取 / 刷新录音列表', command=self.list_files); self.list_button.pack(side='left')
        self.export_button = ttk.Button(actions, text='下载所选', command=self.export_files); self.export_button.pack(side='left', padx=8)
        self.all_button = ttk.Button(actions, text='一键全部下载', command=lambda:self.export_files(all_files=True)); self.all_button.pack(side='left')
        self.delete_button = ttk.Button(actions, text='删除所选…', command=self.delete_files); self.delete_button.pack(side='left', padx=8)
        options = ttk.Frame(files); options.pack(fill='x', pady=(10, 0))
        self.delete_check = ttk.Checkbutton(options, text='下载后删除（完整下载并校验成功后逐条删除设备原件）', variable=self.delete_after)
        self.delete_check.pack(anchor='w')
        location = ttk.Frame(outer); location.pack(fill='x', pady=(12, 0))
        ttk.Label(location, text='固定下载目录：').pack(anchor='w')
        ttk.Label(location, textvariable=self.folder_label, wraplength=820).pack(anchor='w')
        folder_actions = ttk.Frame(location); folder_actions.pack(fill='x', pady=(6, 0))
        self.folder_button = ttk.Button(folder_actions, text='更改目录…', command=self.change_folder); self.folder_button.pack(side='left')
        ttk.Button(folder_actions, text='打开下载目录', command=self.open_output).pack(side='left', padx=8)
        ttk.Progressbar(outer, variable=self.progress, maximum=100).pack(fill='x', pady=(14, 6))
        ttk.Label(outer, textvariable=self.status, wraplength=790, justify='left').pack(anchor='w')
        bottom = ttk.Frame(outer); bottom.pack(fill='x', pady=(12, 0))
        self.cancel_button = ttk.Button(bottom, text='取消当前操作', command=self.cancel, state='disabled'); self.cancel_button.pack(side='left')
        ttk.Button(bottom, text='关闭并返回麦克风', command=self.close).pack(side='right')
        self.refresh_selected()
        self.render_devices()
        self.window.after(100, self.poll)
        if tab == 'files':
            if load_selection(data_root):
                tabs.select(files)
                self.window.after(250, self.list_files)
        if not load_selection(data_root):
            self.window.after(250, self.scan_devices)

    def refresh_selected(self):
        selected = load_selection(self.data_root)
        self.selected.set('当前录音豆：'+selected.get('name','soundcore Work')+'  '+selected['identity_hex'].upper()
                          if selected else '当前尚未选择录音豆。请扫描后选中自己的设备。')

    def render_devices(self):
        """A remembered selection remains visible independently of BLE advertisements."""
        selected = load_selection(self.data_root)
        selected_id = selected['identity_hex'] if selected else None
        found = {item.identity_hex:item for item in self.scan_results}
        self.candidates = {}
        if selected:
            self.candidates[selected_id] = found.get(selected_id) or Candidate(
                selected_id, selected.get('name','soundcore Work'), -999,
                bool(selected.get('bound_advertised',False)))
        self.candidates.update(found)
        self.devices.delete(*self.devices.get_children())
        for identity, item in self.candidates.items():
            if identity == selected_id:
                if identity in found: state = '当前选择 · 本次扫描发现'
                elif self.has_scanned: state = '当前选择 · 本次未发现，仍已记住'
                elif identity == self.connected_identity: state = '当前选择 · 打开面板前已连接'
                else: state = '当前选择 · 已记住，尚未扫描'
            else:
                state = '附近设备 · 尚未选择'
            self.devices.insert('', 'end', iid=identity,
                values=(item.name,identity.upper(),item.rssi if identity in found else '—',state))
        if selected_id:
            self.devices.selection_set(selected_id)
            self.devices.focus(selected_id)

    def start(self, label, operation):
        if self.busy or self.closing:
            return
        self.busy = True
        self.cancel_requested.clear()
        self.status.set(label)
        self.progress.set(0)
        for button in self.operation_controls():
            button.configure(state='disabled')
        self.cancel_button.configure(state='normal')
        def work():
            async def run():
                self.loop = asyncio.get_running_loop()
                self.task = asyncio.current_task()
                if self.cancel_requested.is_set():
                    raise asyncio.CancelledError()
                async with asyncio.timeout(45):
                    while not self.app.service.idle.is_set():
                        await asyncio.sleep(.05)
                return await operation()
            try:
                result = asyncio.run(run())
                self.events.put(('result', result))
            except asyncio.CancelledError:
                self.events.put(('cancelled', None))
            except Exception as error:
                self.events.put(('error', f'{type(error).__name__}: {error}'))
            finally:
                self.loop = self.task = None
                self.events.put(('finished', None))
        threading.Thread(target=work, name='RecorderFiles', daemon=True).start()

    def scan_devices(self):
        async def operation():
            return ('devices', await scan())
        self.start('正在扫描，约 10 秒。不会自动选择或连接其他人的设备。', operation)

    def operation_controls(self):
        return (self.scan_button, self.select_button, self.list_button, self.export_button,
                self.all_button, self.delete_button, self.folder_button, self.delete_check)

    def change_folder(self):
        folder = filedialog.askdirectory(parent=self.window, title='设定固定下载目录',
                                        initialdir=str(self.output_folder if self.output_folder.exists() else self.data_root))
        if folder:
            try:
                set_download_folder(self.data_root, folder)
            except OSError as error:
                messagebox.showerror('无法保存目录', str(error), parent=self.window); return
            self.output_folder = Path(folder)
            self.folder_label.set(str(self.output_folder))
            self.status.set('已保存下载目录，之后下载无需重复选择。')

    def select_device(self):
        if self.busy:
            return
        rows = self.devices.selection()
        if not rows:
            self.status.set('请先选中列表中的录音豆。'); return
        try:
            save_selection(self.data_root, self.candidates[rows[0]])
        except OSError as error:
            messagebox.showerror('无法记住设备',str(error),parent=self.window); return
        self.recordings.clear()
        self.files.delete(*self.files.get_children())
        self.refresh_selected()
        self.render_devices()
        self.status.set('已记住此设备。可切换到“设备内录音”读取列表，或关闭此窗口开始麦克风连接。')

    def identity(self):
        selected = load_selection(self.data_root)
        if not selected:
            self.status.set('请先在“选择录音豆”页扫描并记住自己的设备。')
            return None
        return selected['identity_hex']

    def list_files(self):
        identity = self.identity()
        if not identity:
            return
        self.deleting = False
        async def operation():
            async with RecorderSession(identity) as session:
                return ('files', (await session.list_files(), session.info))
        self.start('正在连接所选录音豆并读取录音列表…', operation)

    def export_files(self, all_files=False):
        identity = self.identity()
        rows = self.files.selection()
        if not identity:
            return
        selected = ([item for item in self.recordings.values() if not item.active] if all_files
                    else [self.recordings[row] for row in rows])
        if not all_files and not selected:
            self.status.set('请先读取列表，再选中要下载的录音。'); return
        if any(item.active for item in selected):
            self.status.set('所选内容包含正在录制的文件。请先停止设备录音并刷新列表。'); return
        delete_after = self.delete_after.get()
        self.deleting = delete_after
        output_folder = self.output_folder
        async def operation():
            completed = []
            try:
                async with RecorderSession(identity) as session:
                    latest = await session.list_files()
                    if any(item.active for item in latest):
                        raise ValueError('录音豆仍在录音。请弹起侧键停止录音并刷新列表，再下载历史录音。')
                    chosen = latest if all_files else selected
                    for index, recording in enumerate(chosen):
                        last_update = [0.0]
                        def progress(received, expected):
                            if time.monotonic()-last_update[0] > .25:
                                last_update[0] = time.monotonic()
                                self.events.put(('progress', (index, len(chosen), received, expected)))
                        result = await download_one(session, recording, output_folder, delete_after, progress)
                        completed.append(result)
                        self.events.put(('saved', result))
                    if delete_after:
                        self.events.put(('updated_files', (await session.list_files(), session.info)))
            finally:
                self.events.put(('batch_count', len(completed)))
            return ('export', completed)
        self.start('正在读取并下载全部已完成录音…' if all_files else f'正在下载 {len(selected)} 条录音…', operation)

    def delete_files(self):
        identity = self.identity()
        selected = [self.recordings[row] for row in self.files.selection()]
        if not identity or not selected:
            self.status.set('请先选中要删除的录音。'); return
        if any(item.active for item in selected):
            self.status.set('不能删除正在录制的文件。请先停止录音并刷新。'); return
        detail = '\n'.join(f'{item.title}（{item.file_id}）' for item in selected[:8])
        if len(selected) > 8: detail += '\n…'
        if not messagebox.askyesno('删除设备录音', f'将从录音豆删除所选 {len(selected)} 条录音。此操作不可恢复。\n\n{detail}\n\n确认删除？', parent=self.window, default='no'):
            return
        self.deleting = True
        async def operation():
            async with RecorderSession(identity) as session:
                for index, recording in enumerate(selected):
                    self.events.put(('notice', f'正在删除第 {index+1}/{len(selected)} 条：{recording.file_id}'))
                    await session.delete(recording.file_id)
                    self.events.put(('removed', recording.file_id))
                self.events.put(('updated_files', (await session.list_files(), session.info)))
                return ('delete', len(selected))
        self.start('正在删除所选设备录音…', operation)

    def cancel(self):
        if self.cancel_requested.is_set():
            return
        self.cancel_requested.set()
        loop, task = self.loop, self.task
        if loop and task and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        self.status.set('正在取消并释放蓝牙连接；此前已完成的文件会保留。')

    def open_output(self):
        self.output_folder.mkdir(parents=True, exist_ok=True)
        os.startfile(self.output_folder)

    def close(self):
        if self.closed:
            return
        self.closing = True
        if self.busy:
            self.cancel(); return
        self.closed = True
        self.window.destroy()
        if self.resume_after and not self.app.closing and load_selection(self.data_root):
            self.app.service.command('resume')
        self.app.manager = None

    def poll(self):
        while True:
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'updated_files':
                kind, data = 'result', ('files', data)
            if kind == 'result':
                result_type, result = data
                if result_type == 'devices':
                    self.scan_results = result
                    self.has_scanned = True
                    self.render_devices()
                    self.status.set(f'本次扫描发现 {len(result)} 只录音豆；当前选择会保留在列表中。' if result else
                        '本次没有发现广播。已记住的录音豆仍显示在列表中；无需重复选择，可唤醒设备后重新扫描。'
                        if load_selection(self.data_root) else '未找到录音豆。请唤醒设备，断开手机 App / 旧电脑连接后重试。')
                elif result_type == 'files':
                    files, info = result
                    self.files.delete(*self.files.get_children())
                    self.recordings = {str(item.file_id):item for item in files}
                    for key, item in self.recordings.items():
                        self.files.insert('', 'end', iid=key, values=(item.title, key, f'{item.size/1024:.1f} KB', '正在录制，暂不可导出' if item.active else '可导出'))
                    self.status.set(f'已读取 {len(files)} 条录音；固件 {info["firmware"]}。请选择文件后导出。')
                elif result_type == 'delete':
                    self.status.set(f'已从设备删除 {result} 条录音。本地已下载的文件保留。')
                else:
                    self.progress.set(100)
                    removed = sum(bool(item.get('deleted_from_device')) for item in result)
                    self.status.set(f'已完整下载 {len(result)} 条 WAV 录音；已删除设备原件 {removed} 条。可打开下载目录播放。')
            elif kind == 'progress':
                index, count, received, expected = data
                self.progress.set(min(99, 100*(index + min(1, received/max(expected, 1)))/count))
                self.status.set(f'正在导出第 {index+1}/{count} 条：已接收 {received/1024:.1f} KB，等待完整性验证…')
            elif kind == 'saved':
                self.app.message(f'已完整导出录音：{Path(data["path"]).name}，{data["seconds"]:.2f} 秒')
            elif kind == 'notice':
                self.status.set(data)
            elif kind == 'removed':
                self.app.message(f'已删除设备录音：{data}')
                if self.files.exists(str(data)): self.files.delete(str(data))
                self.recordings.pop(str(data), None)
            elif kind == 'batch_count':
                self.completed_count = data
            elif kind == 'error':
                self.status.set('操作失败：'+data)
                self.app.message('设备/文件操作失败：'+data)
            elif kind == 'cancelled':
                self.status.set('已取消后续操作；已发送的删除请求可能已生效，请刷新列表核对。本地已下载文件保留。'
                                if self.deleting else '已取消。未完成的临时文件已清理；已完成的 WAV 会保留。')
            elif kind == 'finished':
                self.busy = False
                for button in self.operation_controls():
                    button.configure(state='normal')
                self.cancel_button.configure(state='disabled')
        if self.closing and not self.busy:
            self.close(); return
        if not self.closed:
            self.window.after(100, self.poll)

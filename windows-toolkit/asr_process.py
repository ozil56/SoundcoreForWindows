"""Launch the external ASR interpreter without the frozen app's runtime."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import threading


_launch_lock = threading.Lock()


def worker_path():
    base = Path(__file__).resolve().parent
    return base/'asr_worker'/'asr_worker.py' if getattr(sys, 'frozen', False) else base/'asr_worker.py'


def worker_environment():
    environment = os.environ.copy()
    for name in list(environment):
        if (name.upper().startswith(('PYTHON', '_PYI_')) or
                name.upper() in ('_MEIPASS2', 'TCL_LIBRARY', 'TK_LIBRARY')):
            del environment[name]
    bundle = getattr(sys, '_MEIPASS', None)
    if bundle:
        bundle = Path(bundle).resolve()
        for name in list(environment):
            if name.upper() == 'PATH':
                environment[name] = os.pathsep.join(
                    entry for entry in environment[name].split(os.pathsep)
                    if entry and not Path(entry.strip('"')).resolve().is_relative_to(bundle))
    environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    return environment


@contextmanager
def external_dll_search():
    # SetDllDirectory is process-wide and inherited by child processes. Reset
    # only while creating the child, then restore the bridge's exact setting.
    with _launch_lock:
        if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
            yield
            return
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
        api.GetDllDirectoryW.restype = wintypes.DWORD
        api.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
        api.SetDllDirectoryW.restype = wintypes.BOOL
        size = api.GetDllDirectoryW(0, None)
        directory = ctypes.create_unicode_buffer(size + 1)
        api.GetDllDirectoryW(len(directory), directory)
        if not api.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not api.SetDllDirectoryW(directory.value or None):
                raise ctypes.WinError(ctypes.get_last_error())


def start_worker(python, model, log):
    python = Path(python).resolve()
    worker = worker_path()
    if not worker.is_file():
        raise FileNotFoundError(f'本地转写程序缺失：{worker}')
    environment = worker_environment()
    with external_dll_search():
        return subprocess.Popen(
            [str(python), '-I', '-X', 'utf8', '-u', str(worker), '--model', str(model)],
            cwd=python.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=log, encoding='utf-8', env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)

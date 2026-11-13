# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all
import os
import sys
from pathlib import Path

for variable, directory in [('TCL_LIBRARY','tcl8.6'),('TK_LIBRARY','tk8.6')]:
    location = Path(sys.base_prefix)/'tcl'/directory
    if location.is_dir():
        os.environ.setdefault(variable,str(location))

datas = [('asr_worker.py', '.'), ('tool_settings.py', '.'), ('vocabulary_context.py','.')]
binaries = []
hiddenimports = ['pystray._win32', 'tkinter', 'tkinter.ttk', 'tkinter.messagebox',
                 'tkinter.filedialog', '_tkinter']
hiddenimports += collect_submodules('winrt')
tmp_ret = collect_all('pyogg')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['soundcore_microphone.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SoundcoreMicrophone',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['soundcore-microphone.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SoundcoreMicrophone',
)

"""Portable setup helpers. Uses the current user's Run key, without elevation."""
import json
from pathlib import Path
import platform
import subprocess
import sys
import winreg

RUN_KEY=r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME='SoundcoreMicrophoneBridge'


def startup_command(executable,root,script=None):
    args=[str(executable)]
    if script: args.append(str(script))
    args.extend(['--tray','--data-dir',str(root)])
    command=subprocess.list2cmdline(args)
    if len(command)>260:
        raise ValueError('工具包路径过长，请放到较短的固定路径，例如 D:\\Tools\\Soundcore。')
    return command


class AutoStart:
    def __init__(self,root,registry=winreg):
        self.registry=registry
        if getattr(sys,'frozen',False):
            self.command=startup_command(Path(sys.executable).resolve(),root)
        else:
            pythonw=Path(sys.executable).with_name('pythonw.exe')
            self.command=startup_command(pythonw,root,Path(__file__).with_name('soundcore_microphone.py'))

    def current_value(self):
        try:
            with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER,RUN_KEY) as key:
                return self.registry.QueryValueEx(key,RUN_NAME)[0]
        except FileNotFoundError:
            return ''

    def enabled(self):
        return self.current_value()==self.command

    def set_enabled(self,enabled):
        if enabled:
            with self.registry.CreateKeyEx(self.registry.HKEY_CURRENT_USER,RUN_KEY,0,self.registry.KEY_SET_VALUE) as key:
                self.registry.SetValueEx(key,RUN_NAME,0,self.registry.REG_SZ,self.command)
        elif self.enabled():
            with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER,RUN_KEY,0,self.registry.KEY_SET_VALUE) as key:
                self.registry.DeleteValue(key,RUN_NAME)


def environment_summary(root):
    import sounddevice as sd
    lines=[f'系统：{platform.system()} {platform.release()} / {platform.machine()}']
    try:
        devices=sd.query_devices()
        cable_in=any((d['name'].startswith('CABLE Input') or d['name']=='Output (VB-Audio Point)')
                     and d['max_output_channels']>0 for d in devices)
        cable_out=any(d['name'].startswith('CABLE Output') and d['max_input_channels']>0 for d in devices)
        lines.append('虚拟声卡：已检测到输入和输出端口' if cable_in and cable_out else
                     '虚拟声卡：未就绪。请独立安装 VB-CABLE，重启后重新打开本程序。')
    except Exception as error:
        lines.append(f'虚拟声卡：读取失败（{type(error).__name__}），请检查音频驱动。')
    try:
        data=json.loads((Path(root)/'recorder-identity.json').read_text(encoding='utf-8-sig'))
        identity=bytes.fromhex(data['identity_hex'])
        if len(identity)!=6: raise ValueError('identity length')
        lines.append('录音豆：已记住选择的设备，无需在 Windows 中配对。')
    except (OSError,ValueError,KeyError):
        lines.append('录音豆：尚未选择，请点击“选择录音豆”扫描并选择。')
    return '\n'.join(lines)


from guide_text import GUIDE

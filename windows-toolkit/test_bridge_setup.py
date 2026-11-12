import unittest
from pathlib import Path
from unittest.mock import patch
from bridge_setup import AutoStart, RUN_NAME, startup_command


class FakeRegistry:
    HKEY_CURRENT_USER='current-user'
    KEY_SET_VALUE=2
    REG_SZ=1
    def __init__(self): self.values={}
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def OpenKey(self,*_): return self
    def CreateKeyEx(self,*_): return self
    def QueryValueEx(self,key,name):
        if name not in self.values: raise FileNotFoundError(name)
        return self.values[name],self.REG_SZ
    def SetValueEx(self,key,name,reserved,typ,value): self.values[name]=value
    def DeleteValue(self,key,name): del self.values[name]


class SetupTests(unittest.TestCase):
    def test_startup_targets_new_machine_path_and_preserves_other_copy(self):
        registry=FakeRegistry()
        with patch('bridge_setup.sys.frozen',True,create=True), patch('bridge_setup.sys.executable',r'D:\Tools\录音豆\SoundcoreMicrophone.exe'):
            manager=AutoStart(Path(r'D:\Tools\录音豆'),registry)
        self.assertEqual(manager.command,startup_command(Path(r'D:\Tools\录音豆\SoundcoreMicrophone.exe').resolve(),Path(r'D:\Tools\录音豆')))
        self.assertIn('--tray',manager.command)
        registry.values['unrelated']='keep me'
        registry.values[RUN_NAME]='another copy of the application'
        manager.set_enabled(False)
        self.assertEqual(registry.values[RUN_NAME],'another copy of the application')
        manager.set_enabled(True)
        self.assertTrue(manager.enabled())
        manager.set_enabled(False)
        self.assertFalse(manager.enabled())
        self.assertEqual(registry.values,{'unrelated':'keep me'})

    def test_command_line_roundtrip_for_chinese_spaces_and_ampersand(self):
        import ctypes
        from ctypes import wintypes
        expected=[r'D:\工具 包 & test\SoundcoreMicrophone.exe','--tray','--data-dir',r'D:\工具 包 & test']
        command=startup_command(expected[0],expected[3])
        shell=ctypes.WinDLL('shell32')
        shell.CommandLineToArgvW.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype=ctypes.POINTER(wintypes.LPWSTR)
        kernel=ctypes.WinDLL('kernel32')
        kernel.LocalFree.argtypes=[ctypes.c_void_p]
        count=ctypes.c_int()
        pointer=shell.CommandLineToArgvW(command,ctypes.byref(count))
        try: self.assertEqual([pointer[i] for i in range(count.value)],expected)
        finally: kernel.LocalFree(pointer)

    def test_rejects_run_key_command_over_260_characters(self):
        with self.assertRaises(ValueError):
            startup_command('C:/'+('a'*245)+'/app.exe','D:/tool')


if __name__=='__main__': unittest.main()

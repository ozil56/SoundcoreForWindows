"""Exercise the actual Tk panel and worker completion flow without device mutation."""
import asyncio
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import soundcore_microphone  # also initializes the host Tcl library paths
import tkinter as tk
import device_manager as ui
from recorder_files import Candidate, Recording, save_selection, load_selection, set_download_folder


class PanelTests(unittest.TestCase):
    def wait(self, condition, seconds=3):
        deadline=time.monotonic()+seconds
        while not condition():
            self.root.update()
            if time.monotonic()>deadline: self.fail('UI worker did not finish')
            time.sleep(.01)
        self.root.update()

    def test_first_selection_resumes_and_all_download_uses_saved_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            self.root=tk.Tk(); self.root.withdraw()
            enabled=threading.Event(); idle=threading.Event(); idle.set()
            commands=[]
            def command(name):
                commands.append(name)
                enabled.clear() if name=='pause' else enabled.set()
                idle.set()
            app=SimpleNamespace(root=self.root,service=SimpleNamespace(enabled=enabled,idle=idle,command=command),
                                closing=False,message=lambda *_:None,manager=None)
            candidate=Candidate('010203040506','soundcore Work',-40,False)
            async def fake_scan(): return [candidate]
            async def export(session,recording,folder,delete_after,progress):
                downloads.append((recording.file_id,folder,delete_after))
                return {'path':str(folder/'download.wav'),'seconds':1}
            recordings=[Recording(123,125,160),Recording(124,0,160,True)]
            class Session:
                def __init__(self,*_): self.info={'firmware':'test'}
                async def __aenter__(self): return self
                async def __aexit__(self,*_): pass
                async def list_files(self): return recordings
            try:
                with patch.object(ui,'scan',fake_scan):
                    panel=ui.DeviceManager(app,base); app.manager=panel
                    panel.window.withdraw()
                    self.wait(lambda:bool(panel.candidates) and not panel.busy)
                    panel.devices.selection_set(candidate.identity_hex)
                    panel.select_device()
                    self.assertEqual(load_selection(base)['identity_hex'],candidate.identity_hex)
                    panel.close()
                    self.assertIn('resume',commands,'first selection must start the microphone after closing')
                target=base/'fixed & 中文'; set_download_folder(base,target)
                downloads=[]
                with patch.object(ui,'RecorderSession',Session),patch.object(ui,'download_one',export),\
                     patch.object(ui.filedialog,'askdirectory',side_effect=AssertionError('download must not prompt for a directory')):
                    panel=ui.DeviceManager(app,base,tab='files'); app.manager=panel
                    panel.window.withdraw()
                    self.assertEqual(panel.devices.get_children(),(candidate.identity_hex,))
                    self.assertIn('当前选择',panel.devices.item(candidate.identity_hex,'values')[3])
                    self.wait(lambda:len(panel.recordings)==2 and not panel.busy)
                    self.assertFalse(panel.delete_after.get(),'delete-after-download must default off')
                    panel.export_files(all_files=True)
                    self.wait(lambda:not panel.busy)
                    self.assertEqual(downloads,[],'history download must wait until device stops recording')
                    self.assertIn('仍在录音',panel.status.get())
                    recordings[:]=[recordings[0]]
                    panel.export_files(all_files=True)
                    self.wait(lambda:not panel.busy)
                    self.assertEqual(downloads,[(123,target,False)])
                    self.assertIn('已完整下载 1',panel.status.get())
                    async def empty_scan(): return []
                    with patch.object(ui,'scan',empty_scan):
                        panel.scan_devices()
                        self.wait(lambda:not panel.busy)
                    self.assertEqual(panel.devices.get_children(),(candidate.identity_hex,),
                                     'remembered device must remain visible after an empty scan')
                    self.assertEqual(panel.devices.selection(),(candidate.identity_hex,))
                    self.assertIn('本次未发现',panel.devices.item(candidate.identity_hex,'values')[3])
                    panel.close()
            finally:
                self.root.destroy()


if __name__=='__main__': unittest.main()

import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import asr_process


class ExternalASRTests(unittest.TestCase):
    def test_frozen_parent_cannot_shadow_external_python(self):
        with tempfile.TemporaryDirectory(prefix='asr 隔离 ') as folder:
            bundle = Path(folder)/'_internal'
            worker = bundle/'asr_worker'
            worker.mkdir(parents=True)
            # Mimic the extension/module collision in a release with a different
            # Python patch version. Importing from this directory must fail.
            for name in ('ctypes.py', '_ctypes.py'):
                (bundle/name).write_text("raise RuntimeError('WRONG bundled runtime')", encoding='utf-8')
            (worker/'asr_worker.py').write_text(
                'import ctypes, _ctypes, json, os, sys\n'
                'dll=ctypes.create_unicode_buffer(32768)\n'
                'if sys.platform=="win32": ctypes.windll.kernel32.GetDllDirectoryW(len(dll),dll)\n'
                'print(json.dumps(dict(ctypes=_ctypes.__file__, isolated=sys.flags.isolated, '
                'utf8=sys.flags.utf8_mode, cwd=os.getcwd(), '
                'pythonpath=os.getenv("PYTHONPATH"), tcl=os.getenv("TCL_LIBRARY"), '
                'bootloader=os.getenv("_PYI_APPLICATION_HOME_DIR"), '
                'dll=dll.value)))\n',
                encoding='utf-8')
            dirty = dict(PYTHONPATH=str(bundle), PYTHONHOME=str(bundle), TCL_LIBRARY=str(bundle),
                         _PYI_APPLICATION_HOME_DIR=str(bundle),
                         PATH=str(bundle)+os.pathsep+os.environ.get('PATH',''))
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, '_MEIPASS', str(bundle), create=True), \
                    patch.object(asr_process, '__file__', str(bundle/'asr_process.py')), \
                    patch.dict(os.environ, dirty):
                if sys.platform == 'win32':
                    api = ctypes.windll.kernel32
                    saved = ctypes.create_unicode_buffer(32768)
                    api.GetDllDirectoryW(len(saved), saved)
                    self.assertTrue(api.SetDllDirectoryW(str(bundle)))
                try:
                    with tempfile.TemporaryFile() as log:
                        process = asr_process.start_worker(sys.executable, 'unused', log)
                        output, _ = process.communicate(timeout=30)
                        log.seek(0)
                        self.assertEqual(process.returncode, 0, log.read().decode('utf-8', errors='replace'))
                    result = json.loads(output)
                    self.assertFalse(Path(result['ctypes']).is_relative_to(bundle))
                    self.assertEqual(result['isolated'], 1)
                    self.assertEqual(result['utf8'], 1)
                    self.assertEqual(result['cwd'], str(Path(sys.executable).resolve().parent))
                    self.assertIsNone(result['pythonpath'])
                    self.assertIsNone(result['tcl'])
                    self.assertIsNone(result['bootloader'])
                    self.assertEqual(result['dll'], '')
                    if sys.platform == 'win32':
                        restored = ctypes.create_unicode_buffer(32768)
                        api.GetDllDirectoryW(len(restored), restored)
                        self.assertEqual(restored.value, str(bundle))
                        with tempfile.TemporaryFile() as log, self.assertRaises(FileNotFoundError):
                            asr_process.start_worker(bundle/'missing.exe', 'unused', log)
                        api.GetDllDirectoryW(len(restored), restored)
                        self.assertEqual(restored.value, str(bundle))
                finally:
                    if sys.platform == 'win32':
                        api.SetDllDirectoryW(saved.value or None)


if __name__ == '__main__':
    unittest.main()

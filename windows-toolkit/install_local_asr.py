"""Set up an isolated offline ASR runtime; run with Windows x64 Python 3.13.

Downloads public packages and model weights only. Does not access recordings.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import venv

from tool_settings import update_settings

MODEL_ID = 'Qwen/Qwen3-ASR-1.7B-hf'
MODEL_REVISION = 'bcd2b5b7f32b480ab5790554cfa8347f246a14f3'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--cpu',action='store_true',help='Use CPU-only PyTorch instead of CUDA 12.8')
    args = parser.parse_args()
    root = args.data_dir.resolve()
    runtime = root/'.asr-venv'
    python = runtime/'Scripts'/'python.exe'
    if not python.exists():
        venv.create(runtime,with_pip=True)
    flags = {'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
    # Hugging Face's HTTP client reads environment proxies, while Windows users
    # often configure the proxy only in system settings.
    environment = os.environ.copy()
    for scheme, proxy in urllib.request.getproxies().items():
        if scheme in ('http','https'):
            environment.setdefault(scheme.upper()+'_PROXY',proxy)
    flags['env'] = environment
    def run(*command):
        subprocess.run([str(python),*command],check=True,**flags)
    run('-m','pip','install','--upgrade','pip')
    index = 'https://download.pytorch.org/whl/'+('cpu' if args.cpu else 'cu128')
    run('-m','pip','install','torch==2.11.0','torchaudio==2.11.0','--index-url',index)
    run('-m','pip','install','-r',str(Path(__file__).with_name('asr-requirements.txt')))
    model = root/'models'/'Qwen3-ASR-1.7B-hf'
    command = ('from huggingface_hub import snapshot_download; import sys; '
               'snapshot_download(sys.argv[1],revision=sys.argv[2],local_dir=sys.argv[3],max_workers=4)')
    run('-c',command,MODEL_ID,MODEL_REVISION,str(model))
    update_settings(root,asr_python=str(python),asr_model_dir=str(model))
    print('本地模型已准备完成。请在托盘“自动同步与转写”面板启用并保存设置。')


if __name__=='__main__':
    main()

"""Build a Windows portable release from an explicit source and binary allowlist."""
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import zipfile

from guide_text import GUIDE

PROJECT = Path(__file__).resolve().parent
REPOSITORY = PROJECT.parent
SOURCE_FILES = (
    'soundcore_microphone.py', 'soundcore_stream.py', 'recorder_files.py',
    'device_manager.py', 'bridge_setup.py', 'guide_text.py', 'files_cli.py',
    'microphone-requirements.txt', 'requirements-build.txt', 'VERSION',
    'SoundcoreMicrophone.spec', 'soundcore-microphone.ico', 'build_release.py',
    'README.md', 'THIRD_PARTY_NOTICES.md', 'test_recordings.py',
    'test_bridge_lifecycle.py', 'test_bridge_setup.py', 'test_manager_workflow.py',
    'auto_sync.py', 'asr_worker.py', 'sync_panel.py', 'tool_settings.py',
    'install_local_asr.py', 'asr-requirements.txt', 'test_auto_sync.py',
    'vocabulary.py','vocabulary_context.py','vocabulary_panel.py','recording_catalog.py',
    'local_library_panel.py','test_vocabulary.py',
)
FORBIDDEN_NAMES = {'recorder-identity.json', 'tool-settings.json', 'bridge-status.json', 'sync-state.json'}
FORBIDDEN_SUFFIXES = {'.wav', '.opus', '.bin', '.log', '.apk', '.lnk'}


def sha256(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def validate_public_files(folder):
    for path in folder.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(folder)
        if (path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES
                or {'diagnostics','__pycache__','.venv','.asr-venv','transcription-cache','models'}.intersection(relative.parts)):
            raise ValueError(f'Runtime data or development environment found in release: {relative}')


def collect_licenses(destination):
    destination.mkdir()
    shutil.copytree(PROJECT/'licenses', destination/'bundled-libraries')
    versions = {}
    # Copy available license files from installed distributions, preserving all
    # colliding basenames with a path-derived prefix rather than overwriting one.
    for distribution in metadata.distributions():
        name = distribution.metadata.get('Name','unknown')
        versions[name] = distribution.version
        for item in distribution.files or []:
            if not any(word in Path(str(item)).name.lower() for word in ('license','copying','copyright')):
                continue
            source = Path(distribution.locate_file(item))
            if source.is_file():
                target = destination/name/(hashlib.sha256(str(item).encode()).hexdigest()[:8]+'-'+source.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    python_license = Path(sys.base_prefix)/'LICENSE.txt'
    if not python_license.is_file():
        raise FileNotFoundError('Python LICENSE.txt missing from this runtime')
    shutil.copy2(python_license, destination/'Python-LICENSE.txt')
    return versions


def build():
    if sys.platform != 'win32' or struct.calcsize('P') != 8 or platform.machine().lower() not in ('amd64','x86_64'):
        raise RuntimeError('Build this package using Windows and x64 Python')
    version = (PROJECT/'VERSION').read_text(encoding='utf-8').strip()
    if not version or any(c not in '0123456789.' for c in version):
        raise ValueError('Invalid VERSION')
    release = PROJECT/'release'
    name = f'SoundcoreForWindows-v{version}-windows-x64'
    stage, archive = release/name, release/(name+'.zip')
    if stage.exists() or archive.exists():
        raise FileExistsError(f'This version is already staged: {stage}; keep it or choose a new version')
    dist, work = PROJECT/'dist', PROJECT/'build'
    # PyInstaller --noconfirm can replace its own build/output folders. Resolve
    # and validate both before invoking it; never point it at another workspace.
    for target in (dist, work, release):
        if not target.resolve().is_relative_to(PROJECT) or target.resolve() == PROJECT:
            raise ValueError('Build path escaped project directory')
    environment = os.environ.copy()
    for variable, directory in [('TCL_LIBRARY','tcl8.6'),('TK_LIBRARY','tk8.6')]:
        location = Path(sys.base_prefix)/'tcl'/directory
        if location.is_dir():
            environment.setdefault(variable, str(location))
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
                    '--distpath',str(dist),'--workpath',str(work),'SoundcoreMicrophone.spec'],
                   cwd=PROJECT, env=environment, check=True)
    binary = dist/'SoundcoreMicrophone'
    release.mkdir(exist_ok=True)
    stage.mkdir()
    shutil.copy2(binary/'SoundcoreMicrophone.exe', stage/'SoundcoreMicrophone.exe')
    shutil.copytree(binary/'_internal', stage/'_internal')
    source = stage/'windows-toolkit'; source.mkdir()
    for filename in SOURCE_FILES:
        shutil.copy2(PROJECT/filename, source/filename)
    shutil.copytree(PROJECT/'licenses', source/'licenses')
    for filename in ('README.md','README.upstream.md','PROTOCOL.md'):
        shutil.copy2(REPOSITORY/filename, stage/filename)
    shutil.copy2(PROJECT/'THIRD_PARTY_NOTICES.md',stage/'THIRD_PARTY_NOTICES.md')
    (stage/'使用说明.txt').write_text(GUIDE, encoding='utf-8-sig')
    versions = collect_licenses(stage/'licenses')
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPOSITORY,text=True).strip()
    changed = bool(subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],
                                          cwd=REPOSITORY,text=True).strip())
    (stage/'BUILD_INFO.json').write_text(json.dumps({
        'version':version,'git_revision':revision,'tracked_changes_at_build':changed,
        'python':platform.python_version(),'platform':'windows-x64','packages':versions,
        'source_directory':'windows-toolkit','virtual_audio_driver_included':False,
    },ensure_ascii=False,indent=2),encoding='utf-8')
    validate_public_files(stage)
    paths = sorted(path for path in stage.rglob('*') if path.is_file())
    manifest = stage/'SHA256SUMS.json'
    manifest.write_text(json.dumps({p.relative_to(stage).as_posix():sha256(p) for p in paths},
                                   ensure_ascii=False,indent=2),encoding='utf-8')
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as output:
        for path in paths+[manifest]:
            output.write(path,(Path(stage.name)/path.relative_to(stage)).as_posix())
    with zipfile.ZipFile(archive) as output:
        if output.testzip() is not None:
            raise ValueError('ZIP integrity validation failed')
    checksum = sha256(archive)
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n',encoding='ascii')
    print(json.dumps({'archive':str(archive),'bytes':archive.stat().st_size,'sha256':checksum}))


if __name__ == '__main__':
    build()

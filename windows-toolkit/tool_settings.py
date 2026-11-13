"""Shared, atomic settings updates; no device data in source defaults."""
import json
import os
from pathlib import Path
import threading
import uuid
import datetime as dt

LOCK = threading.RLock()


def read_settings(root):
    with LOCK:
        try:
            value = json.loads((Path(root)/'tool-settings.json').read_text(encoding='utf-8-sig'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def update_settings(root, **changes):
    with LOCK:
        data = read_settings(root)
        data.update(changes)
        write_json(Path(root)/'tool-settings.json', data)
        return data


def transcript_folder(root):
    return Path(read_settings(root).get('transcript_dir') or Path(root)/'转写文本')


def imported_audio_folder(root):
    root = Path(root)
    default = root.parent/'录音'/'导入录音' if root.name == '数据' else root/'导入录音'
    return Path(read_settings(root).get('import_dir') or default)


def diagnostics_folder(root):
    return Path(read_settings(root).get('diagnostics_dir') or Path(root)/'diagnostics')


def cache_folder(root):
    return Path(read_settings(root).get('cache_dir') or Path(root)/'transcription-cache')


def parse_clock(value):
    parsed = dt.datetime.strptime(value, '%H:%M')
    return parsed.hour*60+parsed.minute


def in_schedule(settings, now=None):
    if not settings.get('schedule_enabled', True):
        return True
    start = parse_clock(settings.get('schedule_start', '00:00'))
    end = parse_clock(settings.get('schedule_end', '06:00'))
    now = now or dt.datetime.now()
    minute = now.hour*60+now.minute
    if start == end:
        return True
    return start <= minute < end if start < end else minute >= start or minute < end


def schedule_label(settings):
    if not settings.get('schedule_enabled', True):
        return '全天（设备空闲时）'
    return f"每天 {settings.get('schedule_start','00:00')}–{settings.get('schedule_end','06:00')}（本机时间）"

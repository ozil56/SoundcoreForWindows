"""Read-only Wi-Fi experiment; pauses and resumes the installed tray bridge."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import time
import traceback

from recorder_files import RecorderSession, load_selection, verify_download
from tool_settings import write_json
from wifi_transfer import WifiTransfer


async def run(args, result):
    selection = load_selection(args.data_dir)
    if not selection:
        raise RuntimeError('请先在工具箱中选择录音豆')
    async with RecorderSession(selection['identity_hex']) as session:
        files = await session.list_files()
        result.update(info=session.info, files=[asdict(item) for item in files])
        if any(item.active for item in files):
            raise RuntimeError('录音正在进行，请停止后重试')
        selected = None
        if files:
            selected = next((item for item in files if item.file_id == args.file_id), None) if args.file_id else min(files, key=lambda item: item.size)
            if selected is None:
                raise ValueError('所选录音不存在')
        elif not args.probe_only:
            raise RuntimeError('设备中没有录音，请先录制测试样本')
        if args.compare_ble and selected:
            started = time.monotonic()
            result['ble_export'] = await session.export(selected, args.output/'ble')
            result['ble_export']['transfer_seconds'] = round(time.monotonic()-started, 3)
            verify_download(result['ble_export'])
            write_json(args.report, result)
        started = time.monotonic()
        async with WifiTransfer(session, result) as transfer:
            result['setup_seconds'] = round(time.monotonic()-started, 3)
            if selected and not args.probe_only:
                result['wifi_export'] = await transfer.export(selected, args.output/'wifi')
                verify_download(result['wifi_export'])
                write_json(Path(result['wifi_export']['path']).with_suffix('.wav.json'), result['wifi_export'])
                if result.get('ble_export'):
                    result['wav_sha256_matches_ble'] = result['ble_export']['sha256'] == result['wifi_export']['sha256']
                    if not result['wav_sha256_matches_ble']:
                        raise ValueError('Wi-Fi 与蓝牙下载的 WAV 校验值不同')
        result['wifi_session_seconds'] = round(time.monotonic()-started, 3)
    # Verify that the recorder returns to normal BLE and still has the source.
    async with RecorderSession(selection['identity_hex']) as restored:
        remaining = await restored.list_files()
        result['ble_restored'] = True
        result['source_files_preserved'] = all(item.file_id in {f.file_id for f in remaining} for item in files)
    result['success'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--bridge-exe', type=Path)
    parser.add_argument('--file-id', type=int)
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('--compare-ble', action='store_true')
    args = parser.parse_args()
    args.data_dir = args.data_dir.resolve()
    args.output = args.output.resolve()
    args.report = args.report.resolve()
    result = {'success': False, 'scope': 'read-only transfer; no source deletions'}
    paused = False
    def bridge(command):
        return subprocess.run([str(args.bridge_exe.resolve()), '--data-dir', str(args.data_dir.resolve()),
                               '--command', command], creationflags=subprocess.CREATE_NO_WINDOW,
                              timeout=20, check=True)
    try:
        if args.bridge_exe:
            bridge('pause')
            paused = True
            time.sleep(3)
        asyncio.run(run(args, result))
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        traceback.print_exc()
    finally:
        if paused:
            try:
                bridge('resume')
                result['bridge_resume_requested'] = True
            except Exception as error:
                result['bridge_resume_error'] = str(error)
        write_json(args.report, result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

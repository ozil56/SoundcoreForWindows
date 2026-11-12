"""Optional source-only diagnostic entry point. Pause the tray bridge first."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from recorder_files import RecorderSession, load_selection, scan


async def run(args):
    if args.scan:
        result = {'devices':[asdict(item) for item in await scan()]}
    else:
        selection = load_selection(args.data_dir)
        if not selection:
            raise ValueError('Please select a recorder in the application first')
        async with RecorderSession(selection['identity_hex']) as session:
            files = await session.list_files()
            result = {'info':session.info, 'files':[asdict(item) for item in files]}
            if args.export:
                selected = next(item for item in files if item.file_id == args.export)
                result['export'] = await session.export(selected, args.output)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text, encoding='utf-8')
    print(text, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--scan', action='store_true')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--export', type=int)
    parser.add_argument('--output', type=Path, default=Path('exports'))
    parser.add_argument('--report', type=Path)
    asyncio.run(run(parser.parse_args()))

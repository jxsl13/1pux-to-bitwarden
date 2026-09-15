#!/usr/bin/env python3
"""Interactive launcher for macOS, Linux, and Windows (Python 3.10+)."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import webbrowser

from import_attachments import BASE, Bitwarden, ImportProblem


def mode_arguments(mode, directory=BASE):
    if mode in ('', 'v'):
        return ['--import-items']
    if mode == 'i':
        return ['--import-items', '--apply']
    if mode == 'a':
        args = ['--apply']
        for name in ('mapping.json', 'zuordnung.json'):
            path = directory / name
            if path.is_file():
                args.extend(['--mapping', str(path)])
                break
        return args
    raise ImportProblem('Invalid mode. Choose v, i, or a.')


@contextmanager
def unlocked_session(bw):
    previous = os.environ.get('BW_SESSION')
    try:
        status = bw.run('status')['status']
        if status == 'unauthenticated':
            raise ImportProblem('Sign in first using bw config server SERVER-URL and bw login. See README.md.')
        if status != 'unlocked':
            print('Unlock the Bitwarden CLI when prompted. Your session stays in process memory.', flush=True)
            # Keep stdin/stderr connected to the terminal for the CLI password prompt.
            result = subprocess.run([*bw.command, 'unlock', '--raw'], stdout=subprocess.PIPE, check=False)
            if result.returncode or not result.stdout.strip():
                raise ImportProblem('The Bitwarden CLI could not be unlocked.')
            os.environ['BW_SESSION'] = result.stdout.decode('utf-8').strip()
            if bw.run('status')['status'] != 'unlocked':
                raise ImportProblem('The Bitwarden CLI is still locked.')
        yield
    finally:
        if previous is None:
            os.environ.pop('BW_SESSION', None)
        else:
            os.environ['BW_SESSION'] = previous


def can_open_browser():
    return sys.platform in ('win32', 'darwin') or bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def run(args):
    source = args.source
    if not source:
        source = input('Path to your .1pux export: ').strip()
        if len(source) >= 2 and source[0] == source[-1] and source[0] in ('"', "'"):
            source = source[1:-1]
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise ImportProblem('The source archive does not exist or is not a regular file.')
    mode = args.mode
    if mode is None:
        print('v = Preview the full migration (default)\n'
              'i = Import entries into My vault, then upload attachments\n'
              'a = Only add attachments to existing entries')
        mode = input('Choose a mode: ').strip().lower()
    options = mode_arguments(mode)
    executable = args.bw or shutil.which('bw')
    if not executable:
        raise ImportProblem('Bitwarden CLI not found. Install bw or specify --bw PATH.')
    bw = Bitwarden(executable, 60)
    root = Path(args.work_dir).expanduser().resolve()
    with unlocked_session(bw):
        result = subprocess.run([sys.executable, str(BASE / 'import_attachments.py'), str(source),
                                 '--bw', executable, '--work-dir', str(root), *options], check=False)
    report = root / ('import-report.html' if '--apply' in options else 'preview.html')
    if result.returncode in (0, 2) and report.is_file() and not args.no_browser and can_open_browser():
        try:
            webbrowser.open(report.as_uri())
        except (OSError, webbrowser.Error):
            print('Could not open a browser. Open the report path printed above.')
    print('When finished, run bw lock to lock the CLI.')
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', nargs='?', help='Path to your .1pux export; otherwise prompted')
    parser.add_argument('--mode', choices=('v', 'i', 'a'), help='v = preview, i = full import, a = attachments only')
    parser.add_argument('--bw', help='Path to the Bitwarden CLI (default: PATH)')
    parser.add_argument('--work-dir', default=str(BASE / 'work'), help='Private directory for reports and temporary files')
    parser.add_argument('--no-browser', action='store_true', help='Do not open the HTML report')
    args = parser.parse_args()
    try:
        return run(args)
    except (KeyboardInterrupt, EOFError):
        print('\nInterrupted.', file=sys.stderr)
        return 130
    except (ImportProblem, OSError, ValueError, KeyError) as error:
        message = str(error) if isinstance(error, ImportProblem) else f'{type(error).__name__}: check input and CLI installation.'
        print('Error: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

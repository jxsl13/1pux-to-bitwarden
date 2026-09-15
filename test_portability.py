"""Cross-platform checks using temporary files, synthetic data, and fake CLIs."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import import_attachments as app
import start
from test_import_attachments import FakeBW, archive, source_item


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_lock_blocks_another_process_and_releases_after_error(self):
        code = ('import sys; from import_attachments import work_lock, ImportProblem\n'
                'try:\n'
                ' with work_lock(sys.argv[1]): pass\n'
                'except ImportProblem: sys.exit(23)\n')
        path = self.root / 'migration.lock'
        def child():
            return subprocess.run([sys.executable, '-c', code, str(path)],
                                  cwd=app.BASE, capture_output=True, timeout=30).returncode
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with app.work_lock(path):
                self.assertEqual(child(), 23)
                raise RuntimeError('synthetic')
        self.assertEqual(child(), 0)

    def test_atomic_state_replacement_and_cleanup(self):
        path = self.root / 'state.json'
        app.save_json(path, {'title': '日本語 ü'})
        app.save_json(path, {'title': 'updated ü'})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'title': 'updated ü'})
        with self.assertRaises(TypeError):
            app.save_json(path, {'invalid': object()})
        with patch.object(app.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                app.save_json(path, {'new': True})
        self.assertEqual(list(self.root.iterdir()), [path])
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'title': 'updated ü'})

    def test_windows_names_and_unicode(self):
        for name in ('CON', 'nul.txt', 'COM1.pdf', 'LPT¹.txt', 'NUL .txt', 'trailing.', 'space ',
                     'stream:secret', 'a?b', 'a*b', 'a|b', 'a<b', 'a>b', 'a"b', 'x' * 256):
            with self.subTest(name=name):
                self.assertIsNotNone(app.filename_problem(name, windows=True))
        for name in ('receipt ü.pdf', '日本語.txt', 'COM10.pdf', 'normal & 100%.txt'):
            self.assertIsNone(app.filename_problem(name, windows=True))
        self.assertIsNone(app.filename_problem('colon:name.txt', windows=False))
        for windows in (True, False):
            for name in ('../secret', 'a\\b', '\x00', '..', '', 'line\nfeed'):
                self.assertIsNotNone(app.filename_problem(name, windows=windows))

    def npm_layout(self):
        prefix = self.root / 'CLI with spaces & ü'
        package = prefix / 'node_modules' / '@bitwarden' / 'cli'
        package.mkdir(parents=True)
        entry = package / 'build' / 'bw.js'
        entry.parent.mkdir()
        entry.write_text('// synthetic entry', encoding='utf-8')
        (package / 'package.json').write_text(json.dumps({
            'name': '@bitwarden/cli', 'bin': {'bw': 'build/bw.js'}}), encoding='utf-8')
        shim = prefix / 'bw.cmd'
        shim.write_text('@exit /b 99', encoding='utf-8')
        (prefix / 'node.exe').touch()
        return shim, entry

    def test_npm_wrapper_is_resolved_without_a_shell(self):
        shim, entry = self.npm_layout()
        expected = [str(shim.parent / 'node.exe'), str(entry)]
        self.assertEqual(app.cli_command(shim, windows=True), expected)
        with patch.object(app.shutil, 'which', side_effect=lambda name: str(shim) if name == 'bw' else None):
            self.assertEqual(app.cli_command('bw', windows=True), expected)
        self.assertEqual(app.cli_command(shim, windows=False), [str(shim)])

    def test_unknown_or_escaping_wrapper_fails_closed(self):
        with self.assertRaises(app.ImportProblem):
            app.cli_command(self.root / 'unknown.cmd', windows=True)
        shim, entry = self.npm_layout()
        metadata = entry.parent.parent / 'package.json'
        metadata.write_text(json.dumps({'name': '@bitwarden/cli', 'bin': '../outside.js'}), encoding='utf-8')
        with self.assertRaises(app.ImportProblem):
            app.cli_command(shim, windows=True)

    def test_real_subprocess_preserves_literal_arguments(self):
        script = self.root / 'fake cli ü.py'
        script.write_text('import json, sys; print(json.dumps(sys.argv[1:]))', encoding='utf-8')
        bw = app.Bitwarden(sys.executable, 30)
        bw.command = [sys.executable, str(script)]
        args = ['get', 'space & %PATH% $(echo bad) "quotes" 日本語', 'trailing\\']
        self.assertEqual(bw.run(*args), args)

    def test_windows_invalid_name_blocks_before_import(self):
        source = self.root / 'synthetic.1pux'
        archive(source, [source_item(filename='bad:stream.txt')], {'files/doc1': b'data'})
        bw = FakeBW()
        bw.data = []
        args = SimpleNamespace(source=str(source), work_dir=str(self.root / 'work'), bw='fake',
                               timeout=5, import_items=True, apply=True, mapping=None,
                               organization_id=None, personal_only=False)
        original = app.filename_problem
        with patch.object(app, 'Bitwarden', return_value=bw), \
                patch.object(app, 'filename_problem', side_effect=lambda name: original(name, windows=True)):
            with self.assertRaisesRegex(app.ImportProblem, 'Filename.*Windows'):
                app.execute(args)
        self.assertEqual(bw.import_calls, 0)
        self.assertEqual(bw.uploads, 0)

    @unittest.skipUnless(app.WINDOWS, 'Requires native Windows ACLs')
    def test_windows_directory_and_children_are_private(self):
        root = self.root / 'private & ü folder'
        native_run = subprocess.run
        def checked_run(*args, **kwargs):
            result = native_run(*args, **kwargs)
            # Safe to show diagnostics here: this directory contains only
            # generated fixtures and the child process never invokes bw.
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            return result
        with patch.object(app.subprocess, 'run', side_effect=checked_run):
            app.private_directory(root)
        app.save_json(root / 'state.json', {'synthetic': True})
        child = root / 'child'
        child.mkdir()
        (child / 'attachment.txt').write_text('synthetic', encoding='utf-8')
        script = '''
$ErrorActionPreference = 'Stop'
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$root = [System.IO.Directory]::GetAccessControl($env:ONEPUX_PRIVATE_DIR)
if (!$root.AreAccessRulesProtected) { throw 'Root ACL is not protected' }
$paths = @($env:ONEPUX_PRIVATE_DIR, [System.IO.Path]::Combine($env:ONEPUX_PRIVATE_DIR, 'state.json'),
    [System.IO.Path]::Combine($env:ONEPUX_PRIVATE_DIR, 'child/attachment.txt'))
foreach ($path in $paths) {
    if ([System.IO.Directory]::Exists($path)) {
        $acl = [System.IO.Directory]::GetAccessControl($path)
    } else {
        $acl = [System.IO.File]::GetAccessControl($path)
    }
    $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
    if ($rules.Count -eq 0) { throw 'Missing ACL' }
    foreach ($rule in $rules) {
        if ($rule.IdentityReference.Value -ne $sid -or $rule.AccessControlType -ne 'Allow') {
            throw 'Unexpected access rule'
        }
    }
}
'''
        env = dict(os.environ, ONEPUX_PRIVATE_DIR=str(root))
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                                 base64.b64encode(script.encode('utf-16le')).decode('ascii')],
                                env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_launcher_modes_and_legacy_mapping(self):
        self.assertEqual(start.mode_arguments('v', self.root), ['--import-items'])
        self.assertEqual(start.mode_arguments('i', self.root), ['--import-items', '--apply'])
        mapping = self.root / 'zuordnung.json'
        mapping.write_text('{}', encoding='utf-8')
        self.assertEqual(start.mode_arguments('a', self.root), ['--apply', '--mapping', str(mapping)])
        with self.assertRaises(app.ImportProblem):
            start.mode_arguments('invalid', self.root)

    def test_unlock_session_is_restored_after_failure(self):
        bw = Mock(command=['synthetic-bw'])
        bw.run.side_effect = [{'status': 'locked'}, {'status': 'unlocked'}]
        with patch.dict(os.environ, {'BW_SESSION': 'synthetic-previous'}), \
                patch.object(start.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout=b'synthetic-new')) as run, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                with start.unlocked_session(bw):
                    self.assertEqual(os.environ['BW_SESSION'], 'synthetic-new')
                    raise RuntimeError('synthetic')
            self.assertEqual(os.environ['BW_SESSION'], 'synthetic-previous')
            self.assertEqual(run.call_args.args[0], ['synthetic-bw', 'unlock', '--raw'])
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_launcher_preview_and_no_browser(self):
        source = self.root / 'export & ü.1pux'
        source.touch()
        (self.root / 'preview.html').write_text('synthetic', encoding='utf-8')
        args = SimpleNamespace(source=str(source), mode='v', bw='fake-bw', work_dir=str(self.root), no_browser=True)
        bw = Mock(command=['fake-bw'])
        bw.run.return_value = {'status': 'unlocked'}
        with patch.object(start, 'Bitwarden', return_value=bw), patch.object(start.webbrowser, 'open') as browser, \
                patch.object(start.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(start.run(args), 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertIn(str(source.resolve()), argv)
        self.assertIn('--import-items', argv)
        self.assertNotIn('--apply', argv)
        self.assertNotIn('shell', run.call_args.kwargs)
        browser.assert_not_called()

    def test_linux_headless_does_not_open_browser(self):
        with patch.object(start.sys, 'platform', 'linux'), patch.dict(os.environ, {}, clear=True):
            self.assertFalse(start.can_open_browser())


if __name__ == '__main__':
    unittest.main()

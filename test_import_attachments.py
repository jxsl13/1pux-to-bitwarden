"""Synthetic tests only: never contacts a real vault."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import import_attachments as app


def source_item(title='Example', uid='one', filename='receipt ü.pdf'):
    return {'uuid': uid, 'overview': {'title': title}, 'details': {
        'documentAttributes': {'documentId': 'doc1', 'fileName': filename, 'decryptedSize': 4}}}


def archive(path, items=None, members=None):
    items = [source_item()] if items is None else items
    data = {'accounts': [{'attrs': {'uuid': 'account'}, 'vaults': [
        {'attrs': {'uuid': 'vault', 'name': 'Personal'}, 'items': items}]}]}
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('export.data', json.dumps(data))
        for name, content in (members if members is not None else {'files/doc1___receipt ü.pdf': b'data'}).items():
            z.writestr(name, content)


class FakeBW:
    def __init__(self):
        self.data = [{'id': 'target', 'name': 'Example', 'attachments': []}]
        self.payloads = {}
        self.uploads = 0
        self.fail_after_upload = False
        self.bad_download = False
        self.import_calls = 0
        self.skip_import_keys = set()
        self.interrupt_after_import = False
        self.fail_after_import = False
        self.imported_payloads = []

    def items(self):
        return copy.deepcopy(self.data)

    def run(self, *args, as_json=True):
        if args[0] == 'status':
            return {'status': 'unlocked', 'serverUrl': 'https://example.invalid', 'userId': 'fake'}
        if args[0] == 'sync':
            return None
        if args[:2] == ('import', '1password1pux'):
            payload = json.loads(Path(args[2]).read_text(encoding='utf-8'))
            self.imported_payloads.append(payload)
            self.import_calls += 1
            # Mimic the real importer's accounts[0] behavior and custom string fields.
            for vault in payload['accounts'][0]['vaults']:
                for source in vault['items']:
                    fields = [{'name': f['title'], 'value': f['value']['string']}
                              for section in source['details'].get('sections') or []
                              for f in section.get('fields') or [] if 'string' in f['value']]
                    marker = next(f['value'] for f in fields if f['name'] == app.SOURCE_FIELD)
                    if marker in self.skip_import_keys:
                        continue
                    self.data.append({'id': f'new-{len(self.data)}', 'name': source['overview']['title'],
                                      'fields': fields, 'attachments': []})
            if self.interrupt_after_import:
                self.interrupt_after_import = False
                raise KeyboardInterrupt()
            if self.fail_after_import:
                self.fail_after_import = False
                raise app.ImportProblem('Simulated lost import response')
            return None
        if args[:2] == ('get', 'item'):
            return copy.deepcopy(next(i for i in self.data if i['id'] == args[2]))
        if args[:2] == ('create', 'attachment'):
            path = Path(args[args.index('--file') + 1])
            item = next(i for i in self.data if i['id'] == args[args.index('--itemid') + 1])
            self.uploads += 1
            aid = f'attachment-{self.uploads}'
            self.payloads[aid] = path.read_bytes()
            item['attachments'].append({'id': aid, 'fileName': path.name})
            if self.fail_after_upload:
                self.fail_after_upload = False
                raise app.ImportProblem('Simulated lost response')
            return None
        raise AssertionError(args)

    def remote_hash(self, item_id, attachment_id, temp_root):
        return app.digest(io.BytesIO(b'wrong' if self.bad_download else self.payloads[attachment_id]))


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'test.1pux'
        archive(self.source)
        self.bw = FakeBW()
        self.args = SimpleNamespace(source=str(self.source), apply=False, mapping=None, bw='fake-bw',
                                    timeout=5, work_dir=str(self.root / 'work'),
                                    organization_id=None, personal_only=False)

    def run_import(self):
        with patch.object(app, 'Bitwarden', return_value=self.bw), contextlib.redirect_stdout(io.StringIO()):
            return app.execute(self.args)

    def report(self):
        return json.loads((self.root / 'work' / ('import-report.json' if self.args.apply else 'preview.json')).read_text(encoding='utf-8'))

    def test_preview_never_uploads(self):
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.uploads, 0)
        self.assertEqual(self.report()['attachments'][0]['status'], 'ready')

    def test_upload_and_repeat(self):
        self.args.apply = True
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.report()['attachments'][0]['status'], 'uploaded_verified')
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.uploads, 1)
        self.assertEqual(self.report()['attachments'][0]['status'], 'existing_verified')
        self.assertEqual(list((self.root / 'work').glob('upload-*')), [])

    def test_lost_upload_response_resume(self):
        self.args.apply = True
        self.bw.fail_after_upload = True
        with self.assertRaises(app.ImportProblem):
            self.run_import()
        self.assertEqual(self.report()['attachments'][0]['status'], 'failed_or_unconfirmed')
        self.run_import()
        self.assertEqual(self.bw.uploads, 1)

    def test_same_name_different_content_is_preserved(self):
        self.bw.data[0]['attachments'] = [{'id': 'old', 'fileName': 'receipt ü.pdf'}]
        self.bw.payloads['old'] = b'other'
        self.args.apply = True
        self.run_import()
        self.assertEqual(len(self.bw.data[0]['attachments']), 2)
        self.assertEqual(self.bw.payloads['old'], b'other')

    def test_ambiguous_target_blocks(self):
        self.bw.data.append({'id': 'other', 'name': 'Example'})
        self.args.apply = True
        self.assertEqual(self.run_import(), 2)
        self.assertEqual(self.bw.uploads, 0)

    def test_manual_mapping_resolves_ambiguity(self):
        self.bw.data.append({'id': 'other', 'name': 'Example'})
        mapping = self.root / 'mapping.json'
        mapping.write_text(json.dumps({'account/vault/one': 'target'}))
        self.args.mapping = str(mapping)
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.report()['attachments'][0]['target_id'], 'target')

    def test_many_sources_one_target_blocks(self):
        archive(self.source, [source_item(), source_item(uid='two')])
        self.assertEqual(self.run_import(), 2)
        self.assertTrue(all(r['status'] == 'blocked' for r in self.report()['attachments']))

    def test_missing_file_and_unreferenced_reported(self):
        archive(self.source, members={'files/icon.png': b'icon'})
        self.assertEqual(self.run_import(), 2)
        self.assertEqual(self.report()['unreferenced_files'], ['files/icon.png'])

    def test_path_traversal_blocked(self):
        archive(self.source, [source_item(filename='../outside')])
        self.args.apply = True
        self.assertEqual(self.run_import(), 2)
        self.assertEqual(self.bw.uploads, 0)
        self.assertFalse((self.root / 'outside').exists())

    def test_nested_attachment_and_document(self):
        item = source_item()
        item['details']['sections'] = [{'fields': [{'value': {'file': {
            'fileId': 'doc2', 'fileName': 'second.txt', 'decryptedSize': 0}}}]}]
        archive(self.source, [item], {'files/doc1___receipt ü.pdf': b'data', 'files/doc2___second.txt': b''})
        self.args.apply = True
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.uploads, 2)

    def test_download_verification_failure(self):
        self.args.apply = True
        self.bw.bad_download = True
        with self.assertRaises(app.ImportProblem):
            self.run_import()
        self.assertEqual(self.report()['attachments'][0]['status'], 'failed_or_unconfirmed')

    def test_cli_arguments_are_not_shell_commands(self):
        with patch.object(app.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'{}', stderr=b'')) as run:
            app.Bitwarden('/fake/bw', 12).run('get', 'item', 'id; $(touch bad)')
        self.assertEqual(run.call_args.args[0], ['/fake/bw', 'get', 'item', 'id; $(touch bad)'])
        self.assertNotIn('shell', run.call_args.kwargs)

    @unittest.skipIf(app.WINDOWS, 'Windows uses ACLs instead of POSIX mode bits')
    def test_private_report_permissions(self):
        self.run_import()
        self.assertEqual((self.root / 'work/preview.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'work').stat().st_mode & 0o777, 0o700)

    def test_actual_export_double_underscore_and_icons(self):
        item = source_item()
        attrs = item['details'].pop('documentAttributes')
        item['details']['sections'] = [{'fields': [{'value': {'file': attrs}}]}]
        item['overview']['icons'] = {'detail': {'fileId': 'icon1'}}
        archive(self.source, [item], {'files/doc1__receipt ü.pdf': b'data', 'files/icon1': b'icon'})
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.report()['unreferenced_files'], [])
        self.assertIsNone(self.report()['attachments'][0]['error'])
        self.assertEqual(self.report()['attachments'][0]['status'], 'ready')

    def test_both_separators_for_same_id_remain_ambiguous(self):
        archive(self.source, members={'files/doc1__receipt ü.pdf': b'data', 'files/doc1___receipt ü.pdf': b'data'})
        self.assertEqual(self.run_import(), 2)
        self.assertIsNotNone(self.report()['attachments'][0]['error'])

    def test_no_matching_targets_explained_and_no_empty_mapping(self):
        self.bw.data = [{'id': 'other', 'name': 'Unrelated', 'organizationId': 'org'}]
        self.assertEqual(self.run_import(), 2)
        diagnostic = self.report()['diagnostics']
        self.assertEqual(diagnostic['personal_targets'], 0)
        self.assertTrue(diagnostic['warnings'])
        self.assertEqual(json.loads((self.root / 'work/mapping-suggested.json').read_text(encoding='utf-8')), {})
        self.assertIn('No target', (self.root / 'work/preview.html').read_text(encoding='utf-8'))

    def test_full_sync_requested(self):
        with patch.object(self.bw, 'run', wraps=self.bw.run) as run:
            self.run_import()
        run.assert_any_call('sync', '--force', as_json=False)

    def test_html_escapes_vault_content(self):
        archive(self.source, [source_item(title='<script>alert(1)</script>')])
        self.run_import()
        rendered = (self.root / 'work/preview.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>', rendered)
        self.assertIn('&lt;script&gt;', rendered)

    def test_decryption_errors_are_not_silenced(self):
        with patch.object(app.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout=b'[]', stderr=b'Failed to decrypt cipher')):
            with self.assertRaises(app.ImportProblem):
                app.Bitwarden('/fake/bw', 12).run('list', 'items')

    def full_import(self, apply=True):
        self.args.import_items = True
        self.args.apply = apply
        self.bw.data = [{'id': 'org-existing', 'name': 'Example', 'organizationId': 'org', 'attachments': []}]

    def test_full_import_preview_has_no_mutations(self):
        self.full_import(apply=False)
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.import_calls, 0)
        self.assertEqual(self.bw.uploads, 0)
        self.assertEqual(self.report()['base_import']['planned_items'], 1)
        self.assertEqual(self.report()['attachments'][0]['status'], 'after_base_import')
        self.assertFalse((self.root / 'work/base-import-state.json').exists())

    def test_full_import_then_attachments_then_idempotent_repeat(self):
        self.full_import()
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.import_calls, 1)
        self.assertEqual(self.bw.uploads, 1)
        self.assertEqual(self.bw.data[0]['attachments'], [])
        self.assertEqual(self.report()['attachments'][0]['target_id'], 'new-1')
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.import_calls, 1)
        self.assertEqual(self.bw.uploads, 1)
        self.assertEqual(list((self.root / 'work').glob('base-import-*')), [self.root / 'work/base-import-state.json'])

    def test_identical_titles_use_distinct_source_ids(self):
        archive(self.source, [source_item(), source_item(uid='two')])
        self.full_import()
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.uploads, 2)
        self.assertEqual(len({r['target_id'] for r in self.report()['attachments']}), 2)
        self.args.import_items = False
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.uploads, 2)

    def test_import_interruption_recovers_without_duplicate_items(self):
        self.full_import()
        self.bw.interrupt_after_import = True
        with self.assertRaises(KeyboardInterrupt):
            self.run_import()
        self.assertEqual(self.bw.uploads, 0)
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.import_calls, 1)
        self.assertEqual(self.bw.uploads, 1)

    def test_lost_import_response_verified_from_source_ids(self):
        self.full_import()
        self.bw.fail_after_import = True
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.import_calls, 1)
        self.assertEqual(self.bw.uploads, 1)

    def test_partial_import_requires_review_and_retries_only_missing(self):
        archive(self.source, [source_item(), source_item(uid='two')])
        self.full_import()
        self.bw.skip_import_keys = {'account/vault/two'}
        with self.assertRaises(app.ImportProblem):
            self.run_import()
        self.assertEqual(self.bw.uploads, 0)
        self.bw.skip_import_keys.clear()
        with self.assertRaises(app.ImportProblem):
            self.run_import()
        self.assertEqual(self.bw.import_calls, 1)
        self.args.retry_missing_items = True
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.import_calls, 2)
        self.assertEqual(len(self.bw.imported_payloads[-1]['accounts'][0]['vaults'][0]['items']), 1)
        self.assertEqual(self.bw.uploads, 2)

    def test_full_import_refuses_preexisting_unmarked_personal_items(self):
        self.args.import_items = True
        self.args.apply = True
        with self.assertRaises(app.ImportProblem):
            self.run_import()
        self.assertEqual(self.bw.import_calls, 0)

    def test_full_import_is_bound_to_archive_and_account(self):
        self.full_import()
        self.run_import()
        archive(self.source, [source_item(title='Modified')])
        with self.assertRaises(app.ImportProblem):
            self.run_import()
        self.assertEqual(self.bw.import_calls, 1)

    def test_missing_confirmed_item_is_not_recreated(self):
        self.full_import()
        self.run_import()
        self.bw.data = self.bw.data[:1]
        with self.assertRaises(app.ImportProblem):
            self.run_import()
        self.assertEqual(self.bw.import_calls, 1)

    def test_multiple_accounts_imported_separately(self):
        with zipfile.ZipFile(self.source) as z:
            data = json.loads(z.read('export.data'))
        other = copy.deepcopy(data['accounts'][0])
        other['attrs']['uuid'] = 'account2'
        data['accounts'].append(other)
        with zipfile.ZipFile(self.source, 'w') as z:
            z.writestr('export.data', json.dumps(data))
            z.writestr('files/doc1__receipt ü.pdf', b'data')
        self.full_import()
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.import_calls, 2)
        self.assertEqual(self.bw.uploads, 2)

    def test_legacy_source_fields_and_state_resume_without_duplicates(self):
        self.full_import()
        self.run_import()
        for item in self.bw.data:
            for field in item.get('fields') or []:
                if field['name'] == app.SOURCE_FIELD:
                    field['name'] = '1PUX-Quell-ID'
        current = self.root / 'work/base-import-state.json'
        legacy = self.root / 'work/basisimport-status.json'
        current.rename(legacy)
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.bw.import_calls, 1)
        self.assertEqual(self.bw.uploads, 1)
        self.assertTrue(current.exists())
        self.assertTrue(legacy.exists())

    def test_legacy_state_still_prevents_recreating_missing_items(self):
        self.full_import()
        self.run_import()
        (self.root / 'work/base-import-state.json').rename(self.root / 'work/basisimport-status.json')
        self.bw.data = self.bw.data[:1]
        with self.assertRaises(app.ImportProblem):
            self.run_import()
        self.assertEqual(self.bw.import_calls, 1)

    def test_preview_uses_english_status_and_report(self):
        self.full_import(apply=False)
        self.run_import()
        rendered = (self.root / 'work/preview.html').read_text(encoding='utf-8')
        self.assertIn('lang="en"', rendered)
        self.assertIn('Created during base import', rendered)
        self.assertIn('Attachments are then matched using their source IDs.', rendered)
        self.assertEqual(self.report()['mode'], 'preview')
        self.assertEqual(self.report()['attachments'][0]['status'], 'after_base_import')


if __name__ == '__main__':
    unittest.main()

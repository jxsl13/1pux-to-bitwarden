#!/usr/bin/env python3
"""Import 1Password entries and attachments into Bitwarden (Python 3.10+)."""
import argparse
import copy
from collections import Counter
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile

BASE = Path(__file__).resolve().parent
MAX_FILE = 500 * 1024 * 1024
SOURCE_FIELD = '1PUX Source ID'
# Read-only compatibility with migrations created before the English release.
SOURCE_FIELDS = {SOURCE_FIELD, '1PUX-Quell-ID'}


class ImportProblem(Exception):
    pass


def digest(stream):
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(chunk)
    return h.hexdigest()


def normalized(value):
    return unicodedata.normalize('NFC', str(value or ''))


def save_json(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, path)


def file_references(node):
    """Documents and file fields, including nested value.file objects; never icons."""
    if isinstance(node, dict):
        file_id = node.get('documentId') or node.get('fileId')
        if file_id:
            yield {'id': str(file_id), 'name': node.get('fileName'),
                   'size': node.get('decryptedSize')}
        for value in node.values():
            yield from file_references(value)
    elif isinstance(node, list):
        for value in node:
            yield from file_references(value)


def archive_candidates(files, file_id):
    # 1Password exports in the wild use both ID__name and ID___name.
    return [p for p in files if PurePosixPath(p).name == file_id
            or PurePosixPath(p).name.startswith(file_id + '__')]


def write_review(path, report):
    esc = lambda value: html.escape(str(value or ''))
    diagnostic = report['diagnostics']
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8">',
             '<title>Bitwarden migration preview</title>',
             '<style>body{font:16px system-ui;margin:32px;color:#222}table{border-collapse:collapse;width:100%}'
             'td,th{border:1px solid #ccc;padding:10px;text-align:left;vertical-align:top}'
             'th{background:#eee}p{max-width:1000px}.warning{padding:16px;background:#fff0cd}</style>',
             '<h1>Bitwarden attachment mapping</h1>',
             f'<p>{diagnostic["source_items"]} source items · {diagnostic["target_items"]} Bitwarden targets · '
             f'{len(report["attachments"])} attachments</p>',
             f'<p>Personal targets: {diagnostic["personal_targets"]}; '
             f'Organization targets: {diagnostic["target_items"] - diagnostic["personal_targets"]}.</p>',
             f'<p>Server: {esc(report.get("server"))}<br>Account: {esc(report.get("user_email"))}</p>']
    for warning in diagnostic['warnings']:
        parts.append(f'<p class="warning">{esc(warning)}</p>')
    base = report.get('base_import')
    if base:
        parts.append(f'<p class="warning">Base import into My vault: {base["expected_items"]} total items; '
                     f'{base["confirmed_items"]} already verified; {base["planned_items"]} remaining to create. '
                     'Attachments are then matched using their source IDs.</p>')
    parts.append('<table><tr><th>1Password item / vault</th><th>File</th>'
                 '<th>Bitwarden target</th><th>Status / details</th></tr>')
    for row in report['attachments']:
        parts.append(f'<tr><td>{esc(row["source_title"])}<br>{esc(row["source_vault"])}</td>'
                     f'<td>{esc(row["filename"])}</td><td>{esc(row["target_name"]) or ("Created during base import" if row["status"] == "after_base_import" else "No target")}'
                     f'<br>{esc(row["target_id"])}</td><td>{esc(row["status"])}<br>'
                     f'{esc(row["error"] or row["reason"])}</td></tr>')
    parts.append('</table><p>Details and manual mapping: preview.json / README.md.</p></html>')
    Path(path).write_text('\n'.join(parts), encoding='utf-8')
    os.chmod(path, 0o600)


def read_source(z):
    infos = z.infolist()
    names = [i.filename for i in infos]
    if len(names) != len(set(names)):
        raise ImportProblem('ZIP contains duplicate paths; files cannot be mapped unambiguously.')
    if 'export.data' not in names:
        raise ImportProblem('The 1PUX archive has no export.data at its root.')
    if z.getinfo('export.data').file_size > 256 * 1024 * 1024:
        raise ImportProblem('export.data exceeds 256 MiB.')
    data = json.loads(z.read('export.data'))
    files = [i.filename for i in infos if i.filename.startswith('files/') and not i.is_dir()]
    sources, used, keys = [], set(), set()
    for ai, account in enumerate(data.get('accounts', [])):
        for vi, vault in enumerate(account.get('vaults', [])):
            for ii, item in enumerate(vault.get('items', [])):
                key = '/'.join(str(v) for v in (
                    account.get('attrs', {}).get('uuid') or f'account-{ai}',
                    vault.get('attrs', {}).get('uuid') or f'vault-{vi}',
                    item.get('uuid') or f'item-{ii}'))
                if key in keys:
                    raise ImportProblem('Duplicate source ID in export.data.')
                keys.add(key)
                overview, details = item.get('overview') or {}, item.get('details') or {}
                # Icon blobs are explicitly referenced under overview.icons, not attachments.
                for icon in file_references(overview.get('icons')):
                    used.update(archive_candidates(files, icon['id']))
                source = {'key': key, 'title': overview.get('title') or '',
                          'index': (ai, vi, ii),
                          'vault': vault.get('attrs', {}).get('name', ''),
                          'state': item.get('state', 'active'), 'attachments': [],
                          'username': '', 'urls': set(),
                          'notes': (details.get('notesPlain') or '').replace('\r\n', '\n').rstrip()}
                for field in details.get('loginFields') or []:
                    if field and field.get('designation') == 'username':
                        source['username'] = field.get('value') or ''
                for section in details.get('sections') or []:
                    for field in (section or {}).get('fields') or []:
                        if field and field.get('id') == 'username' and not source['username']:
                            source['username'] = (field.get('value') or {}).get('string') or ''
                source['urls'] = {u['url'] for u in overview.get('urls') or [] if u and u.get('url')}
                if overview.get('url'):
                    source['urls'].add(overview['url'])
                seen = set()
                for ref in file_references(details):
                    if ref['id'] in seen:
                        continue
                    seen.add(ref['id'])
                    candidates = archive_candidates(files, ref['id'])
                    # Never choose arbitrarily between archive entries sharing an ID.
                    attachment = {'file_id': ref['id'], 'filename': ref['name'] or '',
                                  'member': None, 'error': None}
                    if len(candidates) != 1:
                        attachment['error'] = 'Archive file missing or file ID ambiguous'
                    else:
                        member = candidates[0]
                        used.add(member)
                        suffix = PurePosixPath(member).name[len(ref['id']):]
                        name = ref['name'] or (suffix[3:] if suffix.startswith('___') else suffix[2:])
                        attachment.update(member=member, filename=name, size=z.getinfo(member).file_size)
                        if (not isinstance(name, str) or not name or name in ('.', '..')
                                or any(c in name for c in ('/', '\\', '\x00'))
                                or any(ord(c) < 32 for c in name)):
                            attachment['error'] = 'Unsafe or invalid filename'
                        elif attachment['size'] > MAX_FILE:
                            attachment['error'] = 'File exceeds 500 MiB; check the server limit'
                        elif ref['size'] is not None and int(ref['size']) != attachment['size']:
                            attachment['error'] = 'File size does not match export.data'
                    source['attachments'].append(attachment)
                sources.append(source)
    if not sources:
        raise ImportProblem('No items found in export.data.')
    return sources, sorted(set(files) - used)


def marked_targets(items, source_keys):
    mapping = {}
    for item in items:
        markers = [f.get('value') for f in item.get('fields') or []
                   if f.get('name') in SOURCE_FIELDS and f.get('value') in source_keys]
        if len(markers) > 1:
            raise ImportProblem('A target item has multiple 1PUX source IDs; review it before continuing.')
        if not markers:
            continue
        key = markers[0]
        if key in mapping:
            raise ImportProblem('A 1PUX source ID occurs more than once in the target vault; automatic import stopped.')
        if item.get('organizationId'):
            raise ImportProblem('A previously tagged source item belongs to an organization. '
                                'Full import stopped to avoid creating a personal duplicate.')
        mapping[key] = item['id']
    return mapping


def prepare_payload(data, sources):
    """Use the official importer, with one account per file (CLI reads accounts[0])."""
    account_indices = {s['index'][0] for s in sources}
    if len(account_indices) != 1:
        raise ImportProblem('Internal error: an import batch must contain exactly one account.')
    ai = next(iter(account_indices))
    account = {k: copy.deepcopy(v) for k, v in data['accounts'][ai].items() if k != 'vaults'}
    account['vaults'] = []
    for vi in sorted({s['index'][1] for s in sources}):
        original = data['accounts'][ai]['vaults'][vi]
        vault = {k: copy.deepcopy(v) for k, v in original.items() if k != 'items'}
        vault['items'] = []
        for source in sources:
            if source['index'][1] != vi:
                continue
            item = copy.deepcopy(original['items'][source['index'][2]])
            details = item.setdefault('details', {})
            sections = details.get('sections') or []
            if any(f and f.get('title') in SOURCE_FIELDS for section in sections
                   for f in (section or {}).get('fields') or []):
                raise ImportProblem('The export already contains a reserved migration source ID field.')
            sections.append({'title': '', 'name': 'migration_source', 'fields': [
                {'id': 'migration_source_id', 'title': SOURCE_FIELD,
                 'value': {'string': source['key']}, 'guarded': False,
                 'multiline': False, 'dontGenerate': False}]})
            details['sections'] = sections
            vault['items'].append(item)
        account['vaults'].append(vault)
    return {'accounts': [account]}


def import_items(bw, z, sources, items, status, args, root):
    if args.organization_id or args.mapping:
        raise ImportProblem('--import-items uses My vault and source IDs exclusively; '
                            'do not combine it with --organization-id or --mapping.')
    for source in sources:
        for attachment in source['attachments']:
            if attachment['error']:
                raise ImportProblem('Resolve archive file errors before starting a full import.')
    # Check all attachment CRCs before writing any vault entries.
    for member in {a['member'] for s in sources for a in s['attachments']}:
        with z.open(member) as stream:
            digest(stream)
    data = json.loads(z.read('export.data'))
    batches = [[s for s in sources if s['index'][0] == ai]
               for ai in sorted({s['index'][0] for s in sources})]
    # Validate all payloads before starting the first account's import.
    for batch in batches:
        prepare_payload(data, batch)
    source_keys = {s['key'] for s in sources}
    mapping = marked_targets(items, source_keys)
    with Path(args.source).expanduser().open('rb') as stream:
        source_hash = digest(stream)
    binding = {'archive_sha256': source_hash, 'server': status.get('serverUrl'),
               'user_id': status.get('userId')}
    if not binding['server'] or not binding['user_id']:
        raise ImportProblem('Unable to identify the CLI account and server.')
    state_path = root / 'base-import-state.json'
    legacy_path = root / 'basisimport-status.json'
    load_path = state_path if state_path.exists() else legacy_path
    if load_path.exists():
        state = json.loads(load_path.read_text())
        if state.get('binding') != binding:
            raise ImportProblem('The import state belongs to a different archive, account, or server. '
                                'Use a different --work-dir for a separate migration.')
    else:
        # Starting a full migration into an existing unmarked vault risks duplicate credentials.
        unmarked = [i for i in items if not i.get('organizationId') and i['id'] not in mapping.values()]
        if unmarked:
            raise ImportProblem('My vault already contains items without source IDs. '
                                'Automatic initial import stopped; consider attachment-only mode.')
        state = {'version': 1, 'binding': binding, 'expected_keys': sorted(source_keys),
                 'mapping': {}, 'pending': None, 'complete': False}
    lost = set(state.get('mapping', {})) - set(mapping)
    changed = any(mapping.get(key) != value for key, value in state.get('mapping', {}).items())
    if lost or changed:
        raise ImportProblem('Previously verified items are missing or their source IDs have changed. '
                            'Review these items before proceeding; they will not be reimported.')
    pending = state.get('pending')
    if pending:
        unknown_new = {i['id'] for i in items} - set(pending['before_ids']) - set(mapping.values())
        if unknown_new:
            raise ImportProblem('New items without matching source IDs exist after an interrupted import. '
                                'The outcome is uncertain; automatic retry stopped.')
        missing = set(pending['keys']) - set(mapping)
        if missing and not getattr(args, 'retry_missing_items', False):
            raise ImportProblem(f'A previous base import is unconfirmed; {len(missing)} items are missing. '
                                'Existing items will not be imported twice. After reviewing the state and '
                                'ensuring the previous CLI process has finished, resume with --retry-missing-items.')
    missing = source_keys - set(mapping)
    summary = {'expected_items': len(sources), 'confirmed_items': len(mapping),
               'planned_items': len(missing), 'state_file': str(state_path),
               'target': 'My vault', 'complete': not missing}
    if not args.apply:
        return items, mapping, summary
    state.update(mapping=mapping, pending=None, complete=not missing)
    save_json(state_path, state)
    for batch in batches:
        todo = [s for s in batch if s['key'] not in mapping]
        if not todo:
            continue
        print(f'Base import: {len(todo)} items into My vault ...', flush=True)
        state['pending'] = {'keys': [s['key'] for s in todo],
                            'before_ids': [i['id'] for i in items]}
        save_json(state_path, state)  # Write-ahead record before any CLI mutation.
        command_failed = False
        with tempfile.TemporaryDirectory(prefix='base-import-', dir=root) as tmp:
            payload = Path(tmp) / 'export.json'
            save_json(payload, prepare_payload(data, todo))
            try:
                bw.run('import', '1password1pux', str(payload), as_json=False)
            except ImportProblem:
                command_failed = True
        bw.run('sync', '--force', as_json=False)
        items = bw.items()
        mapping = marked_targets(items, source_keys)
        unknown_new = {i['id'] for i in items} - set(state['pending']['before_ids']) - set(mapping.values())
        missing_batch = {s['key'] for s in todo} - set(mapping)
        state['mapping'] = mapping
        save_json(state_path, state)
        if unknown_new or missing_batch:
            raise ImportProblem(f'Base import not fully verified: {len(missing_batch)} expected items are missing, '
                                f'{len(unknown_new)} new items have no matching source ID. '
                                'State saved; attachment uploads and automatic full reimport stopped. '
                                'After reviewing the state, use --retry-missing-items if appropriate.')
        state.update(pending=None, complete=source_keys <= set(mapping))
        save_json(state_path, state)
        if command_failed:
            print('The CLI response was uncertain; all items were verified after synchronization.')
        print(f'Base import verified: {len(mapping)}/{len(sources)} items.', flush=True)
    summary.update(confirmed_items=len(mapping), planned_items=0, complete=True)
    return items, mapping, summary


def choose_target(source, items):
    marked = [i for i in items if any(f.get('name') in SOURCE_FIELDS and f.get('value') == source['key']
                                    for f in i.get('fields') or [])]
    if marked:
        return (marked[0]['id'] if len(marked) == 1 else None,
                'Exact 1PUX source ID' if len(marked) == 1 else 'Duplicate source ID; review required',
                [i['id'] for i in marked])
    candidates = [i for i in items if normalized(i.get('name')) == normalized(source['title'])]
    original = [i['id'] for i in candidates]
    # Exact comparisons only. No fuzzy names, no inference from vault/folder names.
    for key in ('username', 'urls', 'notes'):
        if not source[key]:
            continue
        def agrees(i):
            login = i.get('login') or {}
            if key == 'username':
                return login.get('username') == source[key]
            if key == 'urls':
                return bool(source[key] & {u.get('uri') for u in login.get('uris') or []})
            return (i.get('notes') or '').replace('\r\n', '\n').rstrip() == source[key]
        narrowed = [i for i in candidates if agrees(i)]
        if narrowed:
            candidates = narrowed
    if len(candidates) == 1:
        return candidates[0]['id'], 'Title and available matching fields', original
    return None, 'No unique match; manual mapping required', original


class Bitwarden:
    def __init__(self, executable, timeout):
        self.executable, self.timeout = executable, timeout

    def run(self, *args, as_json=True):
        try:
            p = subprocess.run([self.executable, *args], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            raise ImportProblem('CLI timed out. The upload outcome may be uncertain; rerun to verify.') from None
        if p.returncode:
            # CLI output may contain vault data: do not print or persist it.
            raise ImportProblem(f'bw {args[0]} {args[1] if len(args) > 1 else ""}: '
                                f'Fehlercode {p.returncode}. Check authentication, permissions, and storage.')
        if b'decrypt' in p.stderr.lower() and any(word in p.stderr.lower() for word in (b'fail', b'error', b'unable')):
            raise ImportProblem('The CLI reported decryption errors; the target list may be incomplete. '
                                'Check CLI authentication and synchronization.')
        if not as_json:
            return None
        try:
            return json.loads(p.stdout)
        except (ValueError, UnicodeError):
            raise ImportProblem('The CLI returned invalid JSON; refresh BW_SESSION if necessary.') from None

    def items(self):
        values = self.run('list', 'items') + self.run('list', 'items', '--archived')
        return list({i['id']: i for i in values if not i.get('deletedDate')}.values())

    def remote_hash(self, item_id, attachment_id, temp_root):
        with tempfile.TemporaryDirectory(prefix='verify-', dir=temp_root) as tmp:
            path = Path(tmp) / 'download'
            self.run('get', 'attachment', attachment_id, '--itemid', item_id,
                     '--output', str(path), as_json=False)
            with path.open('rb') as f:
                return digest(f)


def transfer(bw, z, row, temp_root, apply):
    with z.open(row['member']) as stream:
        sha = digest(stream)  # Also verifies the ZIP CRC before an upload.
    row['sha256'] = sha
    item = bw.run('get', 'item', row['target_id'])
    if item.get('deletedDate'):
        raise ImportProblem('The target item has been moved to the trash.')
    existing = item.get('attachments') or []
    for attachment in existing:
        if normalized(attachment.get('fileName')) == normalized(row['filename']):
            if bw.remote_hash(row['target_id'], attachment['id'], temp_root) == sha:
                row.update(status='existing_verified', attachment_id=attachment['id'])
                return
    if not apply:
        row['status'] = 'ready'
        return
    before = {a['id'] for a in existing}
    with tempfile.TemporaryDirectory(prefix='upload-', dir=temp_root) as tmp:
        path = Path(tmp) / row['filename']
        with z.open(row['member']) as src, path.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        with path.open('rb') as f:
            if digest(f) != sha:
                raise ImportProblem('The source file changed while it was being read.')
        row['status'] = 'upload_started'
        bw.run('create', 'attachment', '--file', str(path), '--itemid', row['target_id'], as_json=False)
    updated = bw.run('get', 'item', row['target_id'])
    added = [a for a in updated.get('attachments') or [] if a['id'] not in before
             and normalized(a.get('fileName')) == normalized(row['filename'])]
    for attachment in added:
        if bw.remote_hash(row['target_id'], attachment['id'], temp_root) == sha:
            row.update(status='uploaded_verified', attachment_id=attachment['id'])
            return
    raise ImportProblem('The upload could not be verified by download and SHA-256. Rerun to verify.')


def execute(args):
    os.umask(0o077)
    root = Path(args.work_dir).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with (root / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ImportProblem('An import is already running in this work directory.') from None
        return execute_locked(args, root)


def execute_locked(args, root):
    bw_path = args.bw or shutil.which('bw')
    if not bw_path:
        raise ImportProblem('Bitwarden CLI not found. See README.md or specify --bw /path/to/bw.')
    bw = Bitwarden(bw_path, args.timeout)
    status = bw.run('status')
    if status.get('status') != 'unlocked':
        raise ImportProblem('Bitwarden is locked or logged out. See README.md for bw login and BW_SESSION.')
    mapping = json.loads(Path(args.mapping).read_text()) if args.mapping else {}
    if not isinstance(mapping, dict) or any(not isinstance(v, str) for v in mapping.values()):
        raise ImportProblem('Mapping must be a JSON object of source ID to Bitwarden ID.')
    # A normal sync may skip downloading based on the account revision timestamp.
    bw.run('sync', '--force', as_json=False)
    items = bw.items()
    with zipfile.ZipFile(Path(args.source).expanduser()) as z:
        sources, unreferenced = read_source(z)
        base_report = None
        exact_mapping = {}
        if getattr(args, 'import_items', False):
            items, exact_mapping, base_report = import_items(bw, z, sources, items, status, args, root)
        if args.organization_id:
            items = [i for i in items if i.get('organizationId') == args.organization_id]
        if args.personal_only:
            items = [i for i in items if not i.get('organizationId')]
        by_id = {i['id']: i for i in items}
        unknown_keys = set(mapping) - {s['key'] for s in sources}
        if unknown_keys:
            raise ImportProblem('Mapping contains source IDs that do not belong to this 1PUX archive.')
        assignments = {}
        for source in sources:
            key = source['key']
            if base_report is not None:
                assignments[key] = (exact_mapping.get(key), 'Exact 1PUX source ID' if key in exact_mapping
                                    else 'Item will be created in My vault before uploading attachments', [])
            elif key in mapping:
                target = mapping[key]
                if target and target not in by_id:
                    raise ImportProblem('Mapping contains an unknown or filtered-out Bitwarden ID.')
                assignments[key] = (target or None, 'Manual mapping' if target else 'Manually skipped', [])
            else:
                assignments[key] = choose_target(source, items)
        counts = Counter(t for t, _, _ in assignments.values() if t)
        rows = []
        for source in sources:
            target, reason, candidates = assignments[source['key']]
            if target and counts[target] > 1 and source['key'] not in mapping:
                target, reason = None, 'Multiple source items would share the same target; explicit mapping required'
            for attachment in source['attachments']:
                rows.append({**attachment, 'source_key': source['key'], 'source_title': source['title'],
                             'source_vault': source['vault'], 'source_state': source['state'],
                             'target_id': target, 'target_name': by_id[target]['name'] if target else None,
                             'reason': reason, 'candidate_ids': candidates,
                             'status': ('blocked' if attachment['error'] else 'after_base_import'
                                        if base_report is not None and not target else 'blocked'
                                        if not target else 'unchecked')})
        diagnostic = {'source_items': len(sources), 'target_items': len(items),
                      'personal_targets': sum(not i.get('organizationId') for i in items),
                      'organization_target_counts': dict(Counter(i['organizationId'] for i in items if i.get('organizationId'))),
                      'source_items_with_title_match': sum(bool(c) for _, _, c in assignments.values()),
                      'resolved_attachments': sum(bool(r['target_id']) for r in rows),
                      'missing_archive_files': sum(bool(r['error']) for r in rows), 'warnings': []}
        if rows and not diagnostic['resolved_attachments'] and base_report is None:
            diagnostic['warnings'].append(
                f'None of the {len(rows)} attachments could be assigned to a Bitwarden target. '
                f'The CLI returned {len(items)} targets for {len(sources)} source items. '
                'Check whether the original import exists in the displayed CLI account and server, '
                'whether the required collections are accessible, and whether filters or manual mappings exclude targets. '
                'Attachments cannot be uploaded without matching targets.')
        report = {'mode': 'import' if args.apply else 'preview',
                  'server': status.get('serverUrl'), 'user_id': status.get('userId'),
                  'user_email': status.get('userEmail'), 'diagnostics': diagnostic,
                  'base_import': base_report,
                  'attachments': rows,
                  'unreferenced_files': unreferenced,
                  'unreferenced_note': 'Not automatically mapped; may contain custom icons or unrecognized attachments.',
                  'targets': [{'id': i['id'], 'name': i.get('name'), 'organization_id': i.get('organizationId'),
                               'folder_id': i.get('folderId')} for i in items]}
        report_path = root / ('import-report.json' if args.apply else 'preview.json')
        save_json(report_path, report)
        review_path = root / ('import-report.html' if args.apply else 'preview.html')
        write_review(review_path, report)
        print(f'{len(sources)} source items; {len(items)} Bitwarden targets; {len(rows)} attachments found.')
        if base_report:
            print(f'Base import into My vault: {base_report["confirmed_items"]} verified, '
                  f'{base_report["planned_items"]} remaining to create.')
        print(f'Personal targets: {diagnostic["personal_targets"]}; '
              f'Organization targets: {len(items) - diagnostic["personal_targets"]}.')
        print(f'Archive files with errors: {diagnostic["missing_archive_files"]}; '
              f'Attachments with a target: {diagnostic["resolved_attachments"]}.')
        for warning in diagnostic['warnings']:
            print('NOTICE: ' + warning)
        print(f'Readable report: {review_path}\nDetailed report: {report_path}')
        template = {r['source_key']: r['target_id'] for r in rows if r['target_id']}
        save_json(root / 'mapping-suggested.json', template)
        for n, row in enumerate(rows, 1):
            if row['status'] == 'after_base_import':
                continue
            if row['status'] == 'blocked':
                print(f'[{n}/{len(rows)}] blocked — see report')
                continue
            try:
                transfer(bw, z, row, root, args.apply)
            except ImportProblem as e:
                row.update(status='failed_or_unconfirmed', error=str(e))
                save_json(report_path, report)
                write_review(review_path, report)
                raise
            except (KeyboardInterrupt, OSError):
                row['status'] = 'interrupted_check_status'
                save_json(report_path, report)
                write_review(review_path, report)
                raise
            save_json(report_path, report)
            print(f'[{n}/{len(rows)}] {row["status"]}', flush=True)
        summary = dict(Counter(r['status'] for r in rows))
        report['summary'] = summary
        save_json(report_path, report)
        write_review(review_path, report)
        print('Result: ' + json.dumps(summary, ensure_ascii=False))
        if unreferenced:
            print(f'{len(unreferenced)} unmapped archive files (possibly icons): see report.')
        if not args.apply:
            print('Preview complete. Repeat the same command with --apply to start the import.')
        return 2 if any(r['status'] == 'blocked' for r in rows) or unreferenced else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', help='Path to the existing .1pux archive')
    parser.add_argument('--apply', action='store_true', help='Perform the import and upload attachments; otherwise preview only')
    parser.add_argument('--import-items', action='store_true',
                        help='Import entries into My vault first; with --apply, then upload attachments')
    parser.add_argument('--retry-missing-items', action='store_true',
                        help='After reviewing an interrupted base import, retry only missing entries')
    parser.add_argument('--mapping', help='JSON file mapping source IDs to Bitwarden IDs')
    parser.add_argument('--bw', help='Path to the Bitwarden CLI (default: PATH)')
    parser.add_argument('--work-dir', default=str(BASE / 'work'), help='Private directory for reports and temporary files')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--organization-id', help='Only consider targets in this organization')
    scope.add_argument('--personal-only', action='store_true', help='Only consider personal targets')
    parser.add_argument('--timeout', type=int, default=900, help='Timeout per CLI command in seconds')
    args = parser.parse_args()
    if args.retry_missing_items and not (args.import_items and args.apply):
        parser.error('--retry-missing-items requires --import-items --apply')
    try:
        return execute(args)
    except KeyboardInterrupt:
        print('\nInterrupted. Rerunning will verify existing attachments.', file=sys.stderr)
        return 130
    except (ImportProblem, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as e:
        # Do not echo malformed JSON/source values, which could contain passwords.
        message = str(e) if isinstance(e, ImportProblem) else f'{type(e).__name__}: check input files and access permissions.'
        print('Error: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

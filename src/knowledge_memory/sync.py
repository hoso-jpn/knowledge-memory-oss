"""Explicit allowlisted sync with per-source checkpoints and fail-closed availability."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .core import ID, MAX_NOTE_BYTES, MemoryError, UniqueLoader, parse_note, load_vault
from .notion_api import NOTION_VERSION, _blocks, _full_property, _paginate
from .notion_config import load_config as load_import_config, validate_config
from .notion_import import build as build_notion
from .staging import finalize, plan, write_all
import yaml

GUARD = '.km-source-status.json'
# Outcomes of deciding what to write. Anything else is a fetch, consistency or ownership
# failure, so an unrecognised status counts as failure rather than success.
PLANNED = frozenset({'created', 'revised', 'unchanged', 'locally_modified', 'promoted',
                     'checkpoint_complete'})
SELECTIONS = '.km-sync-selections.json'
NOTION_ID = re.compile(r'(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def digest(value):
    return hashlib.sha256(value).hexdigest()


class FetchError(MemoryError):
    def __init__(self, status):
        self.status = status
        super().__init__(status)  # Never include response bodies, URLs or credentials.


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise FetchError('redirect_refused')


def request(provider, path, method="GET", body=None):
    base = {'github': 'https://api.github.com', 'notion': 'https://api.notion.com/v1'}[provider]
    token = os.environ.get('GITHUB_TOKEN' if provider == 'github' else 'NOTION_TOKEN', '')
    headers = {'Accept': 'application/json', 'User-Agent': 'knowledge-memory-sync'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    if provider == 'notion':
        headers['Notion-Version'] = NOTION_VERSION
    else:
        headers['X-GitHub-Api-Version'] = '2022-11-28'
    if body is not None:
        headers['Content-Type'] = 'application/json'
    for attempt in range(3):
        try:
            req = urllib.request.Request(base + path, headers=headers, method=method,
                                         data=encoded(body).encode() if body is not None else None)
            with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
                raw = response.read(MAX_NOTE_BYTES * 2 + 1)
                if len(raw) > MAX_NOTE_BYTES * 2:
                    raise FetchError('too_large')
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and not exc.headers.get('Retry-After'):
                raise FetchError('inaccessible') from None
            if exc.code == 404:
                raise FetchError('missing_or_inaccessible') from None
            if exc.code == 429 or exc.code >= 500 or exc.headers.get('Retry-After'):
                try:
                    delay = float(exc.headers.get('Retry-After', 2 ** attempt))
                except ValueError:
                    raise FetchError('retry_later') from None
                if not 0 <= delay <= 30 or attempt == 2:
                    raise FetchError('retry_later') from None
                time.sleep(delay)
                continue
            raise FetchError('http_failure') from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == 2:
                raise FetchError('fetch_failed') from None
            time.sleep(2 ** attempt)
    raise FetchError('fetch_failed')


def load_config(path: Path):
    config = yaml.load(path.read_text(), Loader=UniqueLoader)
    if not isinstance(config, dict) or not {'scope', 'sources'} <= config.keys() \
            or config.keys() - {'scope', 'sources', 'selection_id'}:
        raise MemoryError('config requires scope/sources and optional selection_id')
    if not isinstance(config['scope'], str) or not ID.fullmatch(config['scope']):
        raise MemoryError('invalid scope')
    if not isinstance(config['sources'], list) or not 1 <= len(config['sources']) <= 100:
        raise MemoryError('select 1 to 100 explicit sources')
    selection = config.get('selection_id', 'legacy')
    if not isinstance(selection, str) or not ID.fullmatch(selection):
        raise MemoryError('invalid selection_id')
    keys = set()
    for source in config['sources']:
        if not isinstance(source, dict):
            raise MemoryError('invalid source')
        provider = source.get('provider')
        if provider == 'github':
            if set(source) != {'provider', 'repo', 'path', 'ref'}:
                raise MemoryError('github requires repo, path and ref')
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', source['repo']):
                raise MemoryError('invalid repository')
            path_value = source['path']
            if not isinstance(path_value, str) or path_value.startswith('/') or any(
                    p in ('', '.', '..') for p in path_value.split('/')):
                raise MemoryError('select an exact repository file')
            if not isinstance(source['ref'], str) or not source['ref']:
                raise MemoryError('ref is required')
        elif provider == 'notion':
            allowed = {'provider', 'page_id', 'data_source_id', 'body_source', 'body_property',
                       'title_property', 'page_body_slugs', 'import_config'}
            selector = 'data_source_id' if 'data_source_id' in source else 'page_id'
            value = source.get(selector)
            if set(source) - allowed or ('page_id' in source and 'data_source_id' in source) \
                    or not isinstance(value, str) or not NOTION_ID.fullmatch(value):
                raise MemoryError('select one exact Notion page_id or data_source_id')
            if selector == 'data_source_id' and 'selection_id' not in config:
                raise MemoryError('data source sync requires an explicit selection_id')
            if 'import_config' in source:
                if not isinstance(source['import_config'], str):
                    raise MemoryError('import_config must name a configuration file')
                source['import_config'] = load_import_config(path.parent / source['import_config'])
                if any(k in source for k in ('body_property', 'title_property', 'page_body_slugs')) \
                        or source.get('body_source', 'page') != 'page':
                    raise MemoryError('import_config conflicts with body/title/slug settings')
                source['body_source'] = 'page'
            if source.get('body_source') not in ('page', 'property'):
                raise MemoryError('explicit Notion body_source or import_config is required')
            # Hand-maintained publication rules are pinned; a changed configuration starts a new run.
            if 'page_body_slugs' in source and (not isinstance(source['page_body_slugs'], list) or
                    not all(isinstance(s, str) for s in source['page_body_slugs'])):
                raise MemoryError('invalid page_body_slugs')
        else:
            raise MemoryError('provider must be github or notion')
        key = source_key(source)
        if key in keys:
            raise MemoryError('duplicate allowlist source')
        keys.add(key)
    return config


def source_key(source):
    if source['provider'] == 'notion':
        field = 'data_source_id' if 'data_source_id' in source else 'page_id'
        return ('notion-data-source:' if field == 'data_source_id' else 'notion:') + source[field].replace('-', '').lower()
    return 'github:' + source['repo'] + ':' + source['path']


def identifier(source):
    key = source_key(source)
    if source['provider'] != 'notion':
        return 'src-' + digest(key.encode())[:32]
    if 'data_source_id' in source:
        # A data source selects pages; it is not a record and has no note of its own.
        # Without this the key prefix would leak a colon into an otherwise invalid id.
        raise MemoryError('a data source declaration has no note id; enumerate its pages first')
    return 'src-' + key[len('notion:'):]


def fetch_note(source, scope, stamp, fetch=request):
    if source['provider'] == 'github':
        repo = source['repo']
        path = urllib.parse.quote(source['path'], safe='/')
        ref = urllib.parse.quote(source['ref'], safe='')
        data = fetch('github', f'/repos/{repo}/contents/{path}?ref={ref}')
        if data.get('type') != 'file' or data.get('encoding') != 'base64':
            raise FetchError('not_a_text_file')
        try:
            raw = base64.b64decode(data['content'], validate=False)
            body = raw.decode('utf-8')
        except (KeyError, ValueError, UnicodeDecodeError):
            raise FetchError('invalid_text_file') from None
        if len(raw) > MAX_NOTE_BYTES:
            raise FetchError('too_large')
        source_url = f'https://github.com/{repo}/blob/{ref}/{path}'
        meta = dict(id=identifier(source), title=source['path'], kind='source', status='candidate',
                    scope=scope, claim_kind='source_report', updated=stamp,
                    sources=[{'ref': source_url, 'locator': 'whole file'}],
                    origin={'kind': 'github_api', 'record_id': source_key(source),
                            'content_sha256': digest(raw), 'retrieved_at': stamp,
                            'source_url': source_url, 'render_settings': 'sync-v1 ref=' + source['ref']})
        # Git blob SHA is a version, not an edit timestamp; record it in evidence.
        return finalize(meta, body), {'blob_sha': data.get('sha'), 'raw': data}
    page_id = source['page_id']
    page = fetch('notion', f'/pages/{page_id}')
    if not isinstance(page, dict) or page.get('id', '').replace('-', '').lower() != page_id.replace('-', '').lower():
        raise FetchError('invalid_page_response')
    if page.get('archived') or page.get('in_trash'):
        raise FetchError('archived')
    expected_ds = source.get('_data_source_id')
    if expected_ds and (page.get('parent') or {}).get('data_source_id', '').replace('-', '').lower() != expected_ds:
        raise FetchError('moved')
    call = lambda method, path, body=None: fetch('notion', path) if method == 'GET' else fetch('notion', path, method=method, body=body)
    properties = page.get('properties', {})
    full = {name: _full_property(call, page_id, urllib.parse.quote(value['id'], safe=''))
            for name, value in properties.items() if value.get('type') in ('rich_text', 'title')}
    record = {'page': page, 'properties_full': full, 'blocks': _blocks(call, page_id)}
    manifest = {'fetch_finished_at': stamp,
                'schema_properties': {n: v.get('type') for n, v in properties.items()}}
    if expected_ds:
        manifest['data_source_id'] = expected_ds
    if source.get('_database_id'):
        manifest['database_id'] = source['_database_id']
    report = []
    _, _, _, text = build_notion(record, manifest, scope, source['body_source'],
                                 source.get('body_property'), source.get('title_property'),
                                 source.get('page_body_slugs'), import_config=source.get('import_config'),
                                 report=report)
    return text, {'raw': record, 'edited_at': page.get('last_edited_time'), 'conversion_report': report}


def _read_json(path, default):
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MemoryError('invalid sync state') from exc
    if not isinstance(value, dict):
        raise MemoryError('invalid sync state object')
    return value


def _owner(entry):
    return entry.get('selection_id', 'legacy')


def _policy(source):
    return {k: source.get(k) for k in ('body_source', 'body_property', 'title_property',
                                      'page_body_slugs', 'import_config')}


def _enumerate_source(source, fetch):
    ds = source['data_source_id'].replace('-', '').lower()
    schema = fetch('notion', f'/data_sources/{ds}')
    call = lambda method, path, body=None: fetch('notion', path, method=method, body=body)
    pages = _paginate(call, f'/data_sources/{ds}/query', {})
    # Notion documents a 10,000 result query cap. Refuse that boundary rather than
    # declaring a possibly truncated enumeration complete and retiring records.
    if len(pages) >= 10000:
        raise FetchError('enumeration_limit')
    seen = set()
    for page in pages:
        page_id = page.get('id') if isinstance(page, dict) else None
        if not isinstance(page_id, str) or not NOTION_ID.fullmatch(page_id) \
                or page.get('object') != 'page':
            raise FetchError('invalid_enumeration')
        page_id = page_id.replace('-', '').lower()
        if page_id in seen:
            raise FetchError('duplicate_enumeration')
        seen.add(page_id)
    return ds, schema, pages


def _missing_status(page_id, ds, fetch):
    try:
        page = fetch('notion', f'/pages/{page_id}')
        if page.get('archived') or page.get('in_trash'):
            return 'archived'
        parent = (page.get('parent') or {}).get('data_source_id', '').replace('-', '').lower()
        return 'moved' if parent and parent != ds else 'missing_from_data_source'
    except MemoryError as exc:
        return exc.status if isinstance(exc, FetchError) else 'needs_review'


def _checked_journal(config, vault, state, resume):
    journal = _read_json(state / 'checkpoint.json', {})
    selection = config.get('selection_id', 'legacy')
    if resume and (not journal or journal.get('config') != digest(encoded(config).encode()) or
                   journal.get('vault') != str(vault) or
                   journal.get('selection_id', 'legacy') != selection or journal.get('complete')):
        raise MemoryError('no matching interrupted run; start a new sync')
    if journal and (journal.get('vault', str(vault)) != str(vault) or
                    journal.get('selection_id', 'legacy') != selection):
        raise MemoryError('state directory belongs to another vault or selection')
    return journal if resume else {}


def _sync_locked(config, vault, state, dry_run, resume, fetch):
    scope, selection = config['scope'], config.get('selection_id', 'legacy')
    declarations = config['sources']
    fingerprint = digest(encoded(config).encode())
    guard_path, registry_path = vault / GUARD, vault / SELECTIONS
    journal_path = state / 'checkpoint.json'
    guard, registry = _read_json(guard_path, {}), _read_json(registry_path, {})
    for entry in guard.values():
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str)
                                                for k in ('note_id', 'scope', 'status')):
            raise MemoryError('invalid availability ledger')
    containers = sorted(source_key(s) for s in declarations)
    registration = registry.get(selection)
    if registration and (registration.get('scope') != scope or
            any(k.startswith('notion-data-source:') and k not in containers
                for k in registration.get('containers', []))):
        raise MemoryError('scope change or partial data-source selection refused; use the complete profile')
    registry[selection] = {'scope': scope, 'containers': containers}
    journal = _checked_journal(config, vault, state, resume)
    journal = journal or {'config': fingerprint, 'complete': False, 'done': [], 'events': [],
                          'vault': str(vault), 'selection_id': selection, 'enumerations': {}}
    journal.setdefault('enumerations', {})
    target, reports, enumerations = vault / '20-sources', [], []
    existing = load_vault(vault) if vault.exists() else []
    paths = {n.metadata['id']: n.path for n in existing}
    ds_ids = {s['data_source_id'].replace('-', '').lower() for s in declarations if 'data_source_id' in s}
    single_keys = {source_key(s) for s in declarations if 'data_source_id' not in s}

    def claim(source, status='pending'):
        key, note_id = source_key(source), identifier(source)
        previous = guard.get(key)
        if previous and (_owner(previous) != selection or previous['scope'] != scope):
            raise MemoryError('overlapping source ownership; combine selectors in one profile')
        if note_id in paths and paths[note_id] != '20-sources/' + note_id + '.md':
            raise MemoryError('source ID exists outside the managed directory')
        entry = {'note_id': note_id, 'scope': scope, 'status': status, 'selection_id': selection}
        if source.get('_data_source_id'):
            entry['data_source_id'] = source['_data_source_id']
        elif previous and previous.get('data_source_id') in ds_ids:
            entry['data_source_id'] = previous['data_source_id']
        guard[key] = entry
        return key

    # Adopt initial imports only by their recorded source identity; no directory-wide subtraction.
    for note in existing:
        origin = note.metadata.get('origin', {})
        if origin.get('kind') != 'notion_api':
            continue
        ds = origin.get('data_source_id', '').replace('-', '').lower()
        page_id = origin.get('record_id', '').replace('-', '').lower()
        if ds in ds_ids or 'notion:' + page_id in single_keys:
            if note.metadata['scope'] != scope:
                raise MemoryError('imported source belongs to another scope')
            source = {'provider': 'notion', 'page_id': page_id}
            if ds in ds_ids: source['_data_source_id'] = ds
            key = source_key(source)
            if key not in guard:
                claim(source)
            elif _owner(guard[key]) != selection or guard[key]['scope'] != scope:
                raise MemoryError('overlapping imported source ownership')
            elif ds in ds_ids:
                guard[key]['data_source_id'] = ds
    for key, entry in guard.items():
        if _owner(entry) != selection:
            continue
        if entry['scope'] != scope:
            raise MemoryError('selection scope mismatch')
        if entry.get('data_source_id') in ds_ids:
            entry['status'] = 'pending'
            if key in journal['done']: journal['done'].remove(key)
        elif key not in single_keys:
            entry['status'] = 'unselected'
    sources = {source_key(s): dict(s) for s in declarations if 'data_source_id' not in s}
    for key, source in sources.items():
        if key not in journal['done']:
            claim(source)

    def checkpoint():
        if not dry_run:
            write_all([(guard_path, encoded(guard)), (registry_path, encoded(registry)),
                       (journal_path, encoded(journal))])

    checkpoint()  # Every known affected source is held before the first network request.
    enumeration_ok = True
    conflicts = set()
    for declaration in declarations:
        if 'data_source_id' not in declaration:
            continue
        key = source_key(declaration)
        ds = declaration['data_source_id'].replace('-', '').lower()
        stamp = datetime.now(timezone.utc).isoformat()
        journal['enumerations'][key] = {'complete': False, 'config': fingerprint, 'at': stamp}
        checkpoint()
        try:
            ds, schema, pages = _enumerate_source(declaration, fetch)
            member_ids = []
            for page in pages:
                page_id = page['id'].replace('-', '').lower()
                member_ids.append(page_id)
                source = {k: v for k, v in declaration.items() if k != 'data_source_id'}
                source.update(page_id=page_id, _data_source_id=ds)
                db_id = (schema.get('parent') or {}).get('database_id')
                if db_id: source['_database_id'] = db_id
                page_key = source_key(source)
                if page_key in sources and (_policy(source) != _policy(sources[page_key]) or
                        sources[page_key].get('_data_source_id', ds) != ds):
                    conflicts.add(page_key)
                claim(source)
                sources[page_key] = source
            # Missing membership is evaluated only after a complete validated enumeration.
            for page_key, entry in guard.items():
                if _owner(entry) == selection and entry.get('data_source_id') == ds \
                        and page_key[7:] not in member_ids and page_key not in single_keys:
                    entry['status'] = _missing_status(page_key[7:], ds, fetch)
                    reports.append({'source': page_key, 'status': entry['status']})
            evidence = {'schema': schema, 'pages': pages, 'at': stamp, 'data_source_id': ds}
            evidence_text = encoded(evidence)
            journal['enumerations'][key] = {'complete': True, 'config': fingerprint,
                'at': stamp, 'members': sorted(member_ids), 'sha256': digest(evidence_text.encode())}
            if not dry_run:
                write_all([(state / 'enumerations' / (digest(evidence_text.encode()) + '.json'), evidence_text)])
            enumerations.append({'source': key, 'status': 'complete', 'pages': len(pages)})
        except MemoryError as exc:
            enumeration_ok = False
            status = exc.status if isinstance(exc, FetchError) else 'enumeration_failed'
            for page_key, entry in guard.items():
                if _owner(entry) == selection and entry.get('data_source_id') == ds:
                    entry['status'] = status
                    conflicts.add(page_key)  # A simultaneous single-page selector cannot bypass failed DB scope.
            enumerations.append({'source': key, 'status': status})
        checkpoint()
    for key in conflicts:
        if key in guard and guard[key]['status'] == 'pending':
            guard[key]['status'] = 'configuration_conflict'
    checkpoint()
    for key, source in sources.items():
        note_id = identifier(source)
        if key in conflicts:
            reports.append({'source': key, 'status': guard.get(key, {}).get('status', 'configuration_conflict')})
            continue
        if key in journal['done']:
            reports.append({'source': key, 'status': 'checkpoint_complete'})
            continue
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            text, evidence = fetch_note(source, scope, stamp, fetch)
            meta = parse_note(note_id, text.encode()).metadata
            groups = plan(target, {note_id: text}, scope)
            status = next(name for name, ids in groups.items() if note_id in ids)
            guard[key].update(status='current' if status in ('created', 'revised', 'unchanged') else 'needs_review',
                              retrieved_at=stamp, content_sha256=meta['origin']['content_sha256'])
            if meta['origin'].get('semantic_sha256'):
                guard[key]['semantic_sha256'] = meta['origin']['semantic_sha256']
            reports.append({'source': key, 'status': status,
                            'conversion_report': evidence.get('conversion_report', [])})
            if not dry_run:
                raw_text = encoded(evidence)
                items = [(state / 'evidence' / (digest(key.encode()) + '-' + digest(raw_text.encode()) + '.json'), raw_text)]
                if status in ('created', 'revised'):
                    items.append((target / (note_id + '.md'), text))
                journal['done'].append(key)
                journal['events'].append({'source': key, 'status': status, 'at': stamp})
                write_all(items + [(guard_path, encoded(guard)), (journal_path, encoded(journal))])
        except MemoryError as exc:
            status = exc.status if isinstance(exc, FetchError) else 'needs_review'
            guard[key]['status'] = status
            journal['events'].append({'source': key, 'status': status, 'at': stamp})
            reports.append({'source': key, 'status': status})
            checkpoint()
    journal['complete'] = enumeration_ok and not conflicts and all(key in journal['done'] for key in sources)
    checkpoint()
    return {'dry_run': dry_run, 'complete': journal['complete'] if not dry_run else None,
            'selection_id': selection, 'enumerations': enumerations, 'sources': reports}


def sync(config_path: Path, vault: Path, state: Path, dry_run=False, resume=False, fetch=request):
    config = load_config(config_path)
    vault, state = vault.resolve(), state.resolve()
    if state.is_relative_to(vault) or vault.is_relative_to(state):
        raise MemoryError('state and vault must be separate directories')
    # Reject foreign or unbound checkpoints before creating directories or the lock.
    # Revalidate under the lock as well before processing any source.
    _checked_journal(config, vault, state, resume)
    if dry_run:
        return _sync_locked(config, vault, state, True, resume, fetch)
    vault.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    try:
        lock = os.open(vault / '.km-sync.lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise MemoryError('sync lock exists; verify no writer before recovering') from None
    try:
        return _sync_locked(config, vault, state, False, resume, fetch)
    finally:
        os.close(lock)
        (vault / '.km-sync.lock').unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    try:
        result = sync(args.config, args.vault, args.state, args.dry_run, args.resume)
        print(encoded(result))
        if not args.dry_run:
            return 0 if result['complete'] else 2
        # A dry run has no 'complete' flag because nothing is written, but it still learns
        # whether every selected source could be reached. Reporting 0 regardless hid a run
        # in which every page was inaccessible.
        reached = (all(entry['status'] == 'complete' for entry in result['enumerations'])
                   and all(source['status'] in PLANNED for source in result['sources']))
        return 0 if reached else 2
    except (MemoryError, OSError, ValueError):
        print('{"error":"sync refused; check config, paths and checkpoint"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

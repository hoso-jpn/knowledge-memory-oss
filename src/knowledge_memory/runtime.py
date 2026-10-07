"""Opt-in, single-writer sync/index operation and deployment evidence (Linux/POSIX)."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import errno
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

import yaml

from .core import ID, MemoryError, UniqueLoader, build_index, load_vault
from .staging import write_all
from .sync import GUARD, SELECTIONS, load_config, request, source_key, sync

STATUS = 'runtime-status.json'
COMPLETED_SOURCE_STATUSES = {
    'created', 'revised', 'unchanged', 'checkpoint_complete',
    'locally_modified', 'promoted', 'archived', 'moved',
}


def now():
    return datetime.now(timezone.utc)


def _lock_descriptor(path):
    """Open the shared lock inode, including from a read-only vault mount.

    A reader may hold the vault read-only, which is how published snapshots are
    served. flock excludes an exclusive holder through a read-only descriptor as
    well, but the inode cannot be created there, so the file must already exist.
    Never continue without the lock: that would silently drop serialization.
    """
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    try:
        return os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
            raise
        try:
            return os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except OSError:
            raise MemoryError(
                'serialized runtime cannot open .km-runtime.lock in the vault; create it '
                'before publishing a read-only snapshot') from None


@contextmanager
def runtime_lock(vault):
    """Do not unlink this inode: every cooperating process locks the same file."""
    try:
        import fcntl
    except ImportError:
        raise MemoryError('serialized runtime requires POSIX file locks') from None
    fd = _lock_descriptor(Path(vault).resolve() / '.km-runtime.lock')
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MemoryError('memory runtime is busy; retry after sync/index or retrieval finishes') from None
        yield
    finally:
        os.close(fd)


class SerializedTools:
    def __init__(self, adapter):
        self.adapter = adapter

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def call(self, name, arguments):
        with runtime_lock(self.adapter.vault):
            return self.adapter.call(name, arguments)


def read_status(state):
    path = Path(state) / STATUS
    if path.is_symlink():
        raise MemoryError('runtime status must not be a symlink')
    if not path.exists():
        return None
    result = json.loads(path.read_text())
    if (not isinstance(result, dict) or type(result.get('version')) is not int or result['version'] != 1 or
            type(result.get('consecutive_failures')) is not int or result['consecutive_failures'] < 0 or
            result.get('phase') not in ('sync', 'index', 'ready', 'failed')):
        raise MemoryError('invalid runtime status')
    for field in ('binding', 'vault_sha256'):
        if not isinstance(result.get(field), str) or not re.fullmatch('[0-9a-f]{64}', result[field]):
            raise MemoryError('invalid runtime status binding')
    if result.get('next_retry_at'):
        if not isinstance(result['next_retry_at'], str) or datetime.fromisoformat(result['next_retry_at']).tzinfo is None:
            raise MemoryError('invalid retry time')
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def overlap(left, right):
    return (left == right or left.is_relative_to(right) or right.is_relative_to(left) or
            (left.exists() and right.exists() and left.samefile(right)))


def manifest(config=None, commit=None):
    if commit is not None and not re.fullmatch('[0-9a-f]{40}', commit):
        raise MemoryError('commit label must be a full SHA')
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(Path(__file__).parent.glob('*.py'))}
    dependencies = {}
    for name in ('knowledge-memory', 'PyYAML', 'mcp'):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None
    return {'created_at': now().isoformat(), 'python': sys.version.split()[0],
            'sqlite': sqlite3.sqlite_version, 'dependencies': dependencies,
            'source_files_sha256': sources, 'source_manifest_sha256': fingerprint(sources),
            'config_sha256': fingerprint(load_config(Path(config))) if config else None,
            'commit_label': commit, 'commit_verified': False,
            'source_scope': 'package files on disk; not proof of code loaded in an existing process'}


def run(config, vault, database, state, *, resume=False, backoff_seconds=300,
        fetch=request, indexer=build_index):
    if type(backoff_seconds) is not int or not 1 <= backoff_seconds <= 86400:
        raise MemoryError('backoff must be from 1 to 86400 seconds')
    if Path(database).is_symlink():
        raise MemoryError('database must not be a symlink')
    config, vault, database, state = map(lambda p: Path(p).resolve(), (config, vault, database, state))
    settings = load_config(config)
    original = yaml.load(config.read_text(), Loader=UniqueLoader)
    inputs = [config] + [(config.parent / row['import_config']).resolve()
                         for row in original['sources'] if 'import_config' in row]
    destinations = [vault, database, state]
    if (database.suffix != '.sqlite3' or
            any(overlap(current, other) for i, current in enumerate(destinations)
                for other in destinations[:i]) or
            any(overlap(source, target) for source in inputs for target in destinations)):
        raise MemoryError('config, vault, state and database paths are not safely separated')
    binding = fingerprint([str(vault), str(database), settings['scope'], settings.get('selection_id', 'legacy')])
    previous = read_status(state)
    if previous and previous.get('binding') != binding:
        raise MemoryError('runtime state belongs to another profile')
    # Refuse a state directory already bound by sync before adding runtime files there.
    checkpoint = state / 'checkpoint.json'
    if checkpoint.exists():
        journal = json.loads(checkpoint.read_text())
        if journal.get('vault') != str(vault) or journal.get('selection_id', 'legacy') != settings.get('selection_id', 'legacy'):
            raise MemoryError('sync state belongs to another profile')
    vault.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    with runtime_lock(vault):
        previous = read_status(state) or {'version': 1, 'binding': binding, 'consecutive_failures': 0}
        if previous.get('binding') != binding:
            raise MemoryError('runtime state belongs to another profile')
        if previous.get('retry_deferred'):
            raise MemoryError('retry deadline unknown; record a confirmed future retry time before resuming')
        deadline = previous.get('next_retry_at')
        if deadline and now() < datetime.fromisoformat(deadline):
            raise MemoryError('retry backoff has not elapsed')
        status = dict(previous, phase='sync', last_attempt=now().isoformat(),
                      vault_sha256=hashlib.sha256(str(vault).encode()).hexdigest(),
                      config_sha256=fingerprint(settings), next_retry_at=None,
                      retry_deferred=False, failure=None)
        if previous.get('phase') in ('sync', 'index'):
            status['consecutive_failures'] += 1
            status['previous_attempt_interrupted'] = True
        started = time.monotonic()
        calls = 0

        def tracked_fetch(*args, **kwargs):
            nonlocal calls
            calls += 1
            return fetch(*args, **kwargs)

        def save():
            status['logical_api_calls'] = calls
            status['http_attempts_observed'] = False
            write_all([(state / STATUS, json.dumps(status, sort_keys=True, indent=2) + '\n')])

        def failed(reason, deferred=False):
            status.update(phase='failed', failure=reason,
                          consecutive_failures=status['consecutive_failures'] + 1,
                          duration_seconds=time.monotonic() - started, retry_deferred=deferred,
                          next_retry_at=None if deferred else (now() + timedelta(seconds=backoff_seconds)).isoformat())
            save()
            return status

        save()
        try:
            result = sync(config, vault, state, resume=resume, fetch=tracked_fetch)
            counts = Counter(row['status'] for row in result['sources'])
            status['source_counts'] = dict(counts)
            status['enumeration_counts'] = dict(Counter(row['status'] for row in result['enumerations']))
            status['enumerated_pages'] = sum(row.get('pages', 0) for row in result['enumerations'])
            if result['enumerations'] and all(row['status'] == 'complete' for row in result['enumerations']):
                status['last_complete_enumeration'] = now().isoformat()
            retry = 'retry_later' in counts or 'retry_later' in status['enumeration_counts']
            if (not result['complete'] or set(counts) - COMPLETED_SOURCE_STATUSES or
                    set(status['enumeration_counts']) - {'complete'}):
                return failed('sync_incomplete', retry)
            status.update(phase='index', last_sync_success=now().isoformat())
            save()
            count = indexer(vault, database)
            status.update(phase='ready', last_success=now().isoformat(), consecutive_failures=0,
                          indexed_records=count, index_sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
                          duration_seconds=time.monotonic() - started)
            save()
            return status
        except (MemoryError, OSError, ValueError):
            return failed('index_failed' if status['phase'] == 'index' else 'sync_failed')


def _reconcile_inventory(vault):
    """Require a complete managed vault; untracked notes are otherwise readable."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MemoryError('duplicate key in reconcile metadata')
            result[key] = value
        return result

    def read(name):
        path = vault / name
        if path.is_symlink() or not path.is_file():
            raise MemoryError('reconcile requires regular availability ledger and selection registry files')
        result = json.loads(path.read_text(), object_pairs_hook=unique)
        if not isinstance(result, dict):
            raise MemoryError('invalid reconcile metadata')
        return result

    ledger, registry = read(GUARD), read(SELECTIONS)
    by_id = {}
    for key, entry in ledger.items():
        if (not isinstance(entry, dict) or
                not all(isinstance(entry.get(k), str) and entry[k] for k in ('note_id', 'scope', 'status')) or
                not isinstance(entry.get('selection_id', 'legacy'), str) or
                not all(ID.fullmatch(entry.get(k, 'legacy')) for k in ('note_id', 'scope', 'selection_id'))):
            raise MemoryError('invalid availability ledger')
        if re.fullmatch('notion:[0-9a-f]{32}', key):
            expected_id = 'src-' + key[len('notion:'):]
        elif key.startswith('github:'):
            expected_id = 'src-' + hashlib.sha256(key.encode()).hexdigest()[:32]
        else:
            raise MemoryError('invalid ledger source identity')
        if entry['note_id'] != expected_id or entry['note_id'] in by_id:
            raise MemoryError('invalid ledger note identity')
        if 'data_source_id' in entry and (not key.startswith('notion:') or
                not isinstance(entry['data_source_id'], str) or
                not re.fullmatch('[0-9a-f]{32}', entry['data_source_id'])):
            raise MemoryError('invalid ledger data source identity')
        by_id[entry['note_id']] = key
    for name, registration in registry.items():
        if (not ID.fullmatch(name) or not isinstance(registration, dict) or
                not isinstance(registration.get('scope'), str) or not ID.fullmatch(registration['scope']) or
                not isinstance(registration.get('containers'), list) or
                not all(isinstance(k, str) and k for k in registration['containers']) or
                len(set(registration['containers'])) != len(registration['containers'])):
            raise MemoryError('invalid selection registry')
    notes = {note.metadata['id']: note for note in load_vault(vault)}
    for note_id, note in notes.items():
        if note_id not in by_id:
            raise MemoryError('untracked note in managed vault; reconcile cannot certify its availability')
        key = by_id[note_id]
        origin = note.metadata.get('origin', {})
        record_id = origin.get('record_id', '')
        source = ('notion:' + record_id.replace('-', '').lower()
                  if origin.get('kind') == 'notion_api' else record_id
                  if origin.get('kind') == 'github_api' else None)
        if source != key or note.metadata['scope'] != ledger[key]['scope']:
            raise MemoryError('note provenance does not match availability ledger')
    if any(entry['status'] == 'current' and entry['note_id'] not in notes for entry in ledger.values()):
        raise MemoryError('current ledger entry has no corresponding note')
    return ledger, registry, notes


def reconcile(config, vault, current_vault):
    """Compare stopped managed snapshots; never infer current holds from a backup."""
    config, vault, current_vault = map(lambda p: Path(p).resolve(), (config, vault, current_vault))
    if overlap(vault, current_vault) or any(
            (vault / name).exists() and (current_vault / name).exists() and
            (vault / name).samefile(current_vault / name) for name in (GUARD, SELECTIONS)):
        raise MemoryError('restored and current vaults must be independent snapshots')
    settings = load_config(config)
    selection, scope = settings.get('selection_id', 'legacy'), settings['scope']
    selected = {source_key(source) for source in settings['sources']}
    ledger, registry, notes = _reconcile_inventory(vault)
    current, current_registry, current_notes = _reconcile_inventory(current_vault)
    registration = registry.get(selection, {})
    registry_matches = set(registry) == {selection} and registration.get('scope') == scope
    reference_matches = current_registry == {selection: {'scope': scope, 'containers': sorted(selected)}}

    def owned(entry):
        return entry.get('selection_id', 'legacy') == selection and entry['scope'] == scope

    def allowed(key, entry):
        return key in selected or (key.startswith('notion:') and
                                  'notion-data-source:' + entry.get('data_source_id', '') in selected)

    served, retired, foreign, held_now, note_status = [], [], [], [], []
    for generation, entries in (('restored', ledger), ('current', current)):
        for key, entry in sorted(entries.items()):
            record = {'source': key, 'note_id': entry['note_id'], 'status': entry['status'],
                      'selection_id': entry.get('selection_id', 'legacy'), 'scope': entry['scope']}
            if not owned(entry):
                foreign.append(dict(record, generation=generation))
                continue
            if generation != 'restored':
                continue
            if entry['status'] != 'current':
                retired.append(record)
            elif not allowed(key, entry):
                served.append(record)
            else:
                reference = current.get(key)
                if (not reference or not owned(reference) or reference['status'] != 'current' or
                        not allowed(key, reference)):
                    held_now.append(dict(record, current_status=reference['status'] if reference else 'missing'))
                elif notes[entry['note_id']].metadata['status'] != current_notes[entry['note_id']].metadata['status']:
                    note_status.append(record)
    return {'config_selection_id': selection, 'scope': scope,
            'ledger_entries': len(ledger), 'selected_sources': len(selected),
            'restored_registry_matches_profile': registry_matches,
            'current_registry_matches_config': reference_matches,
            'would_be_served_but_unselected': served, 'held_in_ledger': retired,
            'owned_by_another_selection': foreign, 'would_be_served_but_held_now': held_now,
            'would_restore_different_note_status': note_status,
            'reconciled': registry_matches and reference_matches and not (
                served or foreign or held_now or note_status),
            'note': 'compares managed notes, registries and recorded availability against a separate '
                    'operator-supplied current snapshot; no network or writes; does not prove upstream '
                    'freshness, content equality or permission to reconnect readers'}


def release_retry(vault, state, instant):
    deadline = datetime.fromisoformat(instant)
    if deadline.tzinfo is None or deadline <= now():
        raise MemoryError('record a confirmed future time with timezone')
    with runtime_lock(vault):
        status = read_status(state)
        if status and status.get('vault_sha256') != hashlib.sha256(str(Path(vault).resolve()).encode()).hexdigest():
            raise MemoryError('runtime state belongs to another vault')
        if not status or not status.get('retry_deferred'):
            raise MemoryError('no deferred retry to schedule')
        status.update(retry_deferred=False, next_retry_at=deadline.isoformat(),
                      retry_deadline_source='operator supplied; not verified from API header')
        write_all([(Path(state) / STATUS, json.dumps(status, sort_keys=True, indent=2) + '\n')])
        return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('manifest'); p.add_argument('--config', type=Path); p.add_argument('--commit')
    p = sub.add_parser('status'); p.add_argument('--state', type=Path, required=True)
    p = sub.add_parser('reconcile')
    p.add_argument('--config', type=Path, required=True); p.add_argument('--vault', type=Path, required=True)
    p.add_argument('--current-vault', type=Path, required=True)
    p = sub.add_parser('release-retry')
    p.add_argument('--vault', type=Path, required=True); p.add_argument('--state', type=Path, required=True)
    p.add_argument('--not-before', required=True)
    p = sub.add_parser('sync-index')
    for name in ('config', 'vault', 'database', 'state'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--resume', action='store_true'); p.add_argument('--backoff-seconds', type=int, default=300)
    args = parser.parse_args(argv)
    try:
        if args.command == 'manifest': result = manifest(args.config, args.commit)
        elif args.command == 'status': result = read_status(args.state) or {'phase': 'never_run'}
        elif args.command == 'reconcile': result = reconcile(args.config, args.vault, args.current_vault)
        elif args.command == 'release-retry': result = release_retry(args.vault, args.state, args.not_before)
        else:
            result = run(args.config, args.vault, args.database, args.state,
                         resume=args.resume, backoff_seconds=args.backoff_seconds)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == 'reconcile':
            return 0 if result['reconciled'] else 2
        return 2 if result.get('phase') == 'failed' else 0
    except (MemoryError, OSError, ValueError, yaml.YAMLError) as exc:
        print(json.dumps({'error': 'runtime refused', 'reason': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

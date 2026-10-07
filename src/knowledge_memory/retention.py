"""Plan and explicitly apply retention of sync evidence on a stopped local profile."""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

import yaml

from .core import MemoryError, UniqueLoader, snapshot
from .runtime import _reconcile_inventory, overlap
from .sync import GUARD, SELECTIONS, encoded, load_config, source_key

PINS = 'retention-pins.json'
MAX_JSON_BYTES = 128 * 1024 * 1024
HASH = r'[0-9a-f]{64}'
NAMES = {'evidence': re.compile(f'({HASH})-({HASH})\\.json\\Z'),
         'enumerations': re.compile(f'({HASH})\\.json\\Z')}


class RetentionError(MemoryError):
    """Static diagnostic safe to emit without private paths or source content."""


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RetentionError('duplicate retention metadata key')
        result[key] = value
    return result


def safe_path(value, *, directory=False):
    path = Path(os.path.abspath(value))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise RetentionError('retention refuses symlink paths')
    info = path.stat()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise RetentionError('retention requires existing directories')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RetentionError('retention requires regular, unshared files')
    return path


def stamp(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def read_json(path):
    path = safe_path(path)
    before = path.stat()
    if before.st_size > MAX_JSON_BYTES:
        raise RetentionError('retention JSON exceeds size limit')
    with path.open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES or stamp(before) != stamp(path.stat()):
        raise RetentionError('retention input changed during reading')
    return json.loads(raw, object_pairs_hook=unique), digest(raw), stamp(before)


@contextmanager
def retention_lock(vault, *, applying=False):
    """Planning does not create a lock or change vault/state mtimes."""
    import fcntl
    lock = safe_path(vault / '.km-runtime.lock')
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    legacy_fd = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RetentionError('memory runtime is busy') from None
        if (vault / '.km-sync.lock').exists() or (vault / '.km-sync.lock').is_symlink():
            raise RetentionError('sync lock exists; stop all writers before retention')
        if applying:
            # Also exclude older km-sync, which does not participate in flock.
            legacy_fd = os.open(vault / '.km-sync.lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        yield
        if not applying and (vault / '.km-sync.lock').exists():
            raise RetentionError('a legacy writer started during retention planning')
    finally:
        if legacy_fd is not None:
            os.close(legacy_fd)
            (vault / '.km-sync.lock').unlink()
        os.close(fd)


def reference_state(config, vault, state, protected_vaults):
    """Bind evidence to notes, latest reads, checkpoint and explicit backup pins."""
    raw_config = yaml.load(config.read_text(), Loader=UniqueLoader)
    if not isinstance(raw_config, dict) or not isinstance(raw_config.get('sources'), list):
        raise RetentionError('invalid retention sync config')
    inputs = [config]
    for source in raw_config['sources']:
        if isinstance(source, dict) and 'import_config' in source:
            inputs.append(safe_path(config.parent / source['import_config']))
    if any(p.is_relative_to(vault) or p.is_relative_to(state) for p in inputs):
        raise RetentionError('retention config must be outside vault and state')
    settings = load_config(config)
    selection, scope = settings.get('selection_id', 'legacy'), settings['scope']
    expected_registry = {selection: {'scope': scope, 'containers': sorted(map(source_key, settings['sources']))}}
    references, required, held, known = {}, defaultdict(set), set(), set()
    for root in (vault, *protected_vaults):
        for name in (GUARD, SELECTIONS):
            safe_path(root / name)
        ledger, registry, notes = _reconcile_inventory(root)
        if root == vault and registry != expected_registry:
            raise RetentionError('current registry does not match retention profile')
        if any(e.get('selection_id', 'legacy') != selection or e['scope'] != scope for e in ledger.values()):
            raise RetentionError('retention reference belongs to another profile')
        if set(registry) != {selection} or registry[selection]['scope'] != scope:
            raise RetentionError('retention reference registry belongs to another profile')
        files = snapshot(root)
        files.update({name: (root / name).read_bytes() for name in (GUARD, SELECTIONS)})
        references[str(root)] = {name: digest(raw) for name, raw in sorted(files.items())}
        for key, entry in ledger.items():
            if root == vault and entry['status'] == 'current' and not (
                    key in expected_registry[selection]['containers'] or
                    'notion-data-source:' + entry.get('data_source_id', '') in expected_registry[selection]['containers']):
                raise RetentionError('current ledger source is outside the configured selection')
            group = digest(key.encode())
            known.add(group)
            if entry['status'] != 'current':
                held.add(group)  # Keep the complete history of held/retired sources.
            if 'content_sha256' in entry:
                if not isinstance(entry['content_sha256'], str) or not re.fullmatch(HASH, entry['content_sha256']):
                    raise RetentionError('invalid ledger evidence hash')
                required[group].add(entry['content_sha256'])
            note = notes.get(entry['note_id'])
            if note:
                required[group].add(note.metadata['origin']['content_sha256'])
    checkpoint, checkpoint_hash, _ = read_json(state / 'checkpoint.json')
    if (not isinstance(checkpoint, dict) or checkpoint.get('complete') is not True or
            checkpoint.get('vault') != str(vault) or checkpoint.get('selection_id', 'legacy') != selection or
            checkpoint.get('config') != digest(encoded(settings).encode()) or
            not isinstance(checkpoint.get('done'), list) or not isinstance(checkpoint.get('events'), list) or
            not isinstance(checkpoint.get('enumerations'), dict)):
        raise RetentionError('retention requires a matching completed checkpoint; preserve interrupted runs')
    selected_ds = {key for key in expected_registry[selection]['containers'] if key.startswith('notion-data-source:')}
    if (set(checkpoint['enumerations']) != selected_ds or
            not all(isinstance(key, str) for key in checkpoint['done']) or
            len(set(checkpoint['done'])) != len(checkpoint['done'])):
        raise RetentionError('incomplete checkpoint references')
    enumerations = {}
    for key, entry in checkpoint['enumerations'].items():
        if (key not in expected_registry[selection]['containers'] or not isinstance(entry, dict) or
                entry.get('complete') is not True or not isinstance(entry.get('sha256'), str) or
                not re.fullmatch(HASH, entry['sha256'])):
            raise RetentionError('invalid checkpoint enumeration reference')
        enumerations['enumerations/' + entry['sha256'] + '.json'] = key.split(':', 1)[1]
    pins, pins_hash = {}, None
    if (state / PINS).exists() or (state / PINS).is_symlink():
        pin_data, pins_hash, _ = read_json(state / PINS)
        if (not isinstance(pin_data, dict) or set(pin_data) != {'version', 'files'} or
                type(pin_data['version']) is not int or pin_data['version'] != 1 or
                not isinstance(pin_data['files'], dict)):
            raise RetentionError('invalid retention pins')
        pins = pin_data['files']
        for name, value in pins.items():
            parts = name.split('/')
            if (len(parts) != 2 or parts[0] not in NAMES or not NAMES[parts[0]].fullmatch(parts[1]) or
                    not isinstance(value, str) or not re.fullmatch(HASH, value)):
                raise RetentionError('unsafe retention pin')
    reference_hash = fingerprint({'vaults': references, 'config': {str(p): digest(p.read_bytes()) for p in inputs},
                                  'checkpoint': checkpoint_hash, 'pins': pins_hash})
    return reference_hash, required, held, known, enumerations, pins


def inventory(state):
    """One artifact at a time: raw content is checked but never placed in the report."""
    rows, directories = [], {}
    for category, pattern in NAMES.items():
        folder = state / category
        if not folder.exists() and not folder.is_symlink():
            continue
        safe_path(folder, directory=True)
        directories[category] = stamp(folder.stat())[:2]
        for path in sorted(folder.iterdir()):
            match = pattern.fullmatch(path.name)
            if not match:
                raise RetentionError('unexpected file in evidence directory; inspect before retention')
            data, sha, identity = read_json(path)
            if match.groups()[-1] != sha or not isinstance(data, dict):
                raise RetentionError('evidence filename and content disagree')
            if category == 'evidence':
                group, content = match[1], data.get('raw')
                if not isinstance(content, dict):
                    raise RetentionError('invalid raw evidence')
                if 'page' in content:
                    page = content['page']
                    page_id = page.get('id') if isinstance(page, dict) else None
                    page_id = page_id.replace('-', '').lower() if isinstance(page_id, str) else ''
                    if not re.fullmatch('[0-9a-f]{32}', page_id) or digest(('notion:' + page_id).encode()) != group:
                        raise RetentionError('evidence source identity mismatch')
                    content_hash = fingerprint(content)
                elif content.get('type') == 'file' and content.get('encoding') == 'base64':
                    encoded_content = content.get('content')
                    if not isinstance(encoded_content, str):
                        raise RetentionError('invalid encoded evidence content')
                    content_hash = digest(base64.b64decode(''.join(encoded_content.split()), validate=True))
                else:
                    raise RetentionError('unsupported evidence format; retain for manual review')
            else:
                ds = data.get('data_source_id')
                if not isinstance(ds, str) or not re.fullmatch('[0-9a-f]{32}', ds) or not isinstance(data.get('pages'), list):
                    raise RetentionError('invalid enumeration evidence')
                group, content_hash = ds, None
            rows.append({'path': category + '/' + path.name, 'category': category, 'group': group,
                         'sha256': sha, 'content_sha256': content_hash, 'stamp': identity,
                         'bytes': identity[2], 'mtime_ns': identity[3]})
    return rows, directories


def make_plan(config, vault, state, *, keep_days, keep_latest, budget_bytes, protected_vaults, as_of):
    before = reference_state(config, vault, state, protected_vaults)
    reference_hash, required, held, known, enumerations, pins = before
    rows, directories = inventory(state)
    by_path = {r['path']: r for r in rows}
    if (any(name not in by_path or by_path[name]['group'] != ds for name, ds in enumerations.items()) or
            any(name not in by_path or by_path[name]['sha256'] != sha for name, sha in pins.items())):
        raise RetentionError('missing or altered checkpoint/pinned evidence')
    available = defaultdict(set)
    groups = defaultdict(list)
    for row in rows:
        groups[row['category'], row['group']].append(row)
        if row['category'] == 'evidence':
            available[row['group']].add(row['content_sha256'])
    if any(not values <= available[group] for group, values in required.items()):
        raise RetentionError('unresolved note/ledger evidence reference; recover or pin original evidence first')
    newest = set()
    for group_rows in groups.values():
        newest.update(r['path'] for r in sorted(group_rows, key=lambda r: (r['mtime_ns'], r['path']), reverse=True)[:keep_latest])
    cutoff_ns = int((as_of - timedelta(days=keep_days)).timestamp() * 1_000_000_000)
    for row in rows:
        reasons = []
        if row['path'] in pins: reasons.append('explicit_pin')
        if row['path'] in enumerations: reasons.append('checkpoint')
        if row['path'] in newest: reasons.append('latest_per_source')
        if row['mtime_ns'] >= cutoff_ns: reasons.append('within_retention_period')
        if row['category'] == 'evidence':
            if row['group'] not in known: reasons.append('unknown_source')
            if row['group'] in held: reasons.append('held_source_history')
            if row['content_sha256'] in required.get(row['group'], set()): reasons.append('note_or_ledger_reference')
        elif row['group'] not in enumerations.values():
            reasons.append('unknown_data_source')
        row['protected_by'] = reasons
        row['action'] = 'keep' if reasons else 'delete'
    if reference_state(config, vault, state, protected_vaults) != before:
        raise RetentionError('retention references changed while planning')
    summary = {}
    for category in NAMES:
        subset = [r for r in rows if r['category'] == category]
        summary[category] = {'files': len(subset), 'bytes': sum(r['bytes'] for r in subset),
                             'oldest_mtime_ns': min((r['mtime_ns'] for r in subset), default=None),
                             'newest_mtime_ns': max((r['mtime_ns'] for r in subset), default=None),
                             'delete_files': sum(r['action'] == 'delete' for r in subset),
                             'delete_bytes': sum(r['bytes'] for r in subset if r['action'] == 'delete')}
    retained = sum(r['bytes'] for r in rows if r['action'] == 'keep')
    plan = {'version': 1, 'as_of': as_of.isoformat(), 'config': str(config), 'vault': str(vault), 'state': str(state),
            'protected_vaults': list(map(str, protected_vaults)),
            'policy': {'keep_days': keep_days, 'keep_latest': keep_latest, 'budget_bytes': budget_bytes},
            'root_inodes': [stamp(p.stat())[:2] for p in (vault, state)], 'directories': directories,
            'reference_sha256': reference_hash, 'artifacts': rows, 'summary': summary,
            'retained_bytes': retained, 'budget_exceeded_after_plan': budget_bytes is not None and retained > budget_bytes,
            'untouched': ['notes', 'ledger', 'registry', 'checkpoint', 'indexes', 'backups', 'published generations', 'other state files']}
    plan['plan_sha256'] = fingerprint(plan)
    return plan


def parameters(config, vault, state, protected_vaults, keep_days, keep_latest, budget_bytes):
    if (type(keep_days) is not int or not 1 <= keep_days <= 36500 or
            type(keep_latest) is not int or not 1 <= keep_latest <= 100000 or
            (budget_bytes is not None and (type(budget_bytes) is not int or budget_bytes < 1))):
        raise RetentionError('retention requires positive keep-days, keep-latest and optional budget-bytes')
    config = safe_path(config)
    vault, state = (safe_path(p, directory=True) for p in (vault, state))
    protected_vaults = [safe_path(p, directory=True) for p in protected_vaults]
    roots = [vault, state, *protected_vaults]
    if any(overlap(a, b) for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise RetentionError('retention directories must be independent')
    for root in protected_vaults:
        if (root / '.km-sync.lock').exists() or (root / '.km-sync.lock').is_symlink():
            raise RetentionError('protected snapshots must be stopped')
    return config, vault, state, protected_vaults


def plan_retention(config, vault, state, *, keep_days, keep_latest=2, budget_bytes=None, protected_vaults=(), as_of=None):
    config, vault, state, protected_vaults = parameters(config, vault, state, protected_vaults, keep_days, keep_latest, budget_bytes)
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise RetentionError('retention time requires a timezone')
    with retention_lock(vault):
        plan = make_plan(config, vault, state, keep_days=keep_days, keep_latest=keep_latest, budget_bytes=budget_bytes,
                         protected_vaults=protected_vaults, as_of=as_of)
    return {'dry_run': True, 'disk_free_bytes': shutil.disk_usage(state).free, 'plan': plan}


def apply_retention(document, expected_sha256):
    if not isinstance(document, dict) or not isinstance(document.get('plan'), dict):
        raise RetentionError('supply a saved retention plan')
    plan = document['plan']
    if not isinstance(expected_sha256, str) or not re.fullmatch(HASH, expected_sha256) or plan.get('plan_sha256') != expected_sha256:
        raise RetentionError('explicit plan hash does not match')
    policy = plan['policy']
    config, vault, state, protected = parameters(plan['config'], plan['vault'], plan['state'], plan['protected_vaults'], **policy)
    as_of = datetime.fromisoformat(plan['as_of'])
    if as_of.tzinfo is None or not timedelta(0) <= datetime.now(timezone.utc) - as_of <= timedelta(days=1):
        raise RetentionError('retention plan must be from the last 24 hours')
    removed = []
    with retention_lock(vault, applying=True):
        current = make_plan(config, vault, state, protected_vaults=protected, as_of=as_of, **policy)
        if current != plan or current['plan_sha256'] != expected_sha256:
            raise RetentionError('retention plan is stale or altered; create and inspect a new plan')
        for row in current['artifacts']:
            if row['action'] != 'delete':
                continue
            fd = None
            try:
                folder = safe_path(state / row['category'], directory=True)
                fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                name = Path(row['path']).name
                if (stamp(os.fstat(fd))[:2] != current['directories'][row['category']] or
                        stamp(os.stat(name, dir_fd=fd, follow_symlinks=False)) != row['stamp']):
                    raise RetentionError('artifact changed after retention validation')
                os.unlink(name, dir_fd=fd)
                removed.append(row['path'])
            except (OSError, MemoryError):
                return {'completed': False, 'plan_sha256': expected_sha256, 'removed': removed,
                        'failed': row['path'], 'reason': 'deletion stopped; inspect and create a new plan'}
            finally:
                if fd is not None:
                    os.close(fd)
    return {'completed': True, 'plan_sha256': expected_sha256, 'removed': removed,
            'removed_bytes': sum(r['bytes'] for r in current['artifacts'] if r['action'] == 'delete'),
            'budget_exceeded': current['budget_exceeded_after_plan']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan', help='read-only inventory and deletion preview; no automatic cleanup')
    for name in ('config', 'vault', 'state'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--keep-days', type=int, required=True)
    p.add_argument('--keep-latest', type=int, default=2)
    p.add_argument('--budget-bytes', type=int)
    p.add_argument('--protect-vault', type=Path, action='append', default=[])
    p = sub.add_parser('apply')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--expect-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan':
            result = plan_retention(args.config, args.vault, args.state, keep_days=args.keep_days,
                                    keep_latest=args.keep_latest, budget_bytes=args.budget_bytes, protected_vaults=args.protect_vault)
        else:
            document, _, _ = read_json(args.plan)
            result = apply_retention(document, args.expect_sha256)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('completed') is False else 0
    except RetentionError as exc:
        print(json.dumps({'error': 'retention refused', 'reason': str(exc)}), file=sys.stderr)
        return 2
    except (MemoryError, OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        # Paths and raw parsing errors can contain private material; no traceback on the CLI.
        print(json.dumps({'error': 'retention refused; inspect inputs and create a fresh plan'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

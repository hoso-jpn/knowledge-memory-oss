"""Read-only deployment preflight for separately operated memory profiles."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import yaml

from .core import ID, MemoryError, UniqueLoader
from .sync import NOTION_ID, load_config, source_key


def overlap(left, right):
    return (left == right or left.is_relative_to(right) or right.is_relative_to(left) or
            (left.exists() and right.exists() and left.samefile(right)))


def load_profiles(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    value = yaml.load(raw, Loader=UniqueLoader)
    if (not isinstance(value, dict) or set(value) != {'version', 'profiles'} or
            type(value['version']) is not int or value['version'] != 1 or
            not isinstance(value['profiles'], list) or not value['profiles']):
        raise MemoryError('profiles require version 1 and a nonempty profiles list')
    names, scopes, selections, owners, destinations, configs = set(), set(), set(), {}, [], []
    result = []
    for row in value['profiles']:
        required = {'name', 'scope', 'vault', 'database', 'state', 'sync_config', 'include_candidates'}
        if not isinstance(row, dict) or set(row) != required:
            raise MemoryError('profile fields do not match version 1')
        for key, seen in (('name', names), ('scope', scopes)):
            text = row[key]
            if not isinstance(text, str) or not ID.fullmatch(text) or text in seen:
                raise MemoryError('profile names and scopes must be valid and distinct')
            seen.add(text)
        if type(row['include_candidates']) is not bool:
            raise MemoryError('include_candidates must be an explicit boolean')
        item = dict(row)
        for key in ('vault', 'database', 'state', 'sync_config'):
            if not isinstance(row[key], str) or not row[key].strip():
                raise MemoryError('profile paths must be explicit')
            item[key] = (path.parent / row[key]).resolve()
        if item['database'].suffix != '.sqlite3':
            raise MemoryError('profile index must be a .sqlite3 file')
        paths = [item[key] for key in ('vault', 'database', 'state')]
        for i, current in enumerate(paths):
            if any(overlap(current, other) for other in paths[:i] + destinations):
                raise MemoryError('profile vault, index and state destinations must not overlap')
        destinations.extend(paths)
        config = load_config(item['sync_config'])
        original = yaml.load(item['sync_config'].read_text(), Loader=UniqueLoader)
        for source in original['sources']:
            if 'import_config' in source:
                configs.append((item['sync_config'].parent / source['import_config']).resolve())
        selection = config.get('selection_id')
        if config['scope'] != item['scope'] or not selection or selection in selections:
            raise MemoryError('profile scope and unique explicit selection_id must match sync config')
        selections.add(selection)
        keys = []
        for source in config['sources']:
            if 'data_source_id' in source:
                raise MemoryError('boundary profiles require exact pages; whole data sources need a separate boundary review')
            # GitHub owner/repository names are case-insensitive. Keep existing
            # sync record IDs unchanged; canonicalize only boundary ownership.
            identity = dict(source, repo=source['repo'].lower()) if source['provider'] == 'github' else source
            key = source_key(identity)
            if key in owners:
                raise MemoryError('one source is selected by multiple profiles')
            owners[key] = row['name']; keys.append(key)
        item.update(sources=keys, selection_id=selection)
        # Includes the resolved import mapping, not just the outer file path.
        item['config_sha256'] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        configs.append(item['sync_config'])
        result.append(item)
    if any(overlap(config, dest) for config in configs + [path] for dest in destinations):
        raise MemoryError('configuration must be outside managed destinations')
    return result


def plan(path, inventory=None):
    profiles = load_profiles(path)
    selected = {key for row in profiles for key in row['sources']}
    inventory_keys = set()
    if inventory is not None:
        values = json.loads(Path(inventory).read_text())
        if not isinstance(values, list):
            raise MemoryError('inventory must be a JSON list of explicit Notion page IDs')
        for value in values:
            if not isinstance(value, str) or not NOTION_ID.fullmatch(value):
                raise MemoryError('invalid inventory page ID')
            inventory_keys.add('notion:' + value.replace('-', '').lower())
    return {'dry_run': True, 'network_requests': 0, 'writes': 0,
            'inventory_supplied': inventory is not None,
            'held_unselected': sorted(inventory_keys - selected),
            'profiles': [{'name': row['name'], 'scope': row['scope'],
                          'selection_id': row['selection_id'],
                          'selected_count': len(row['sources']),
                          'include_candidates': row['include_candidates'],
                          'config_sha256': row['config_sha256']} for row in profiles],
            'live_deployment_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, help='local list of page IDs; never queries Notion')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(plan(args.config, args.inventory), ensure_ascii=False, indent=2))
        return 0
    except (MemoryError, OSError, ValueError, yaml.YAMLError):
        print('{"error":"profile preflight refused; inspect boundaries and explicit allowlists"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

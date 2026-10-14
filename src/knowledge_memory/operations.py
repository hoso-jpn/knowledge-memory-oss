"""Explicit backup, recovery, measurement and local MCP verification. No scheduler."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
try:
    import resource
except ImportError:  # Windows has no resource module.
    resource = None
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .core import (MAX_NOTE_BYTES, MemoryError, build_index, get_note, index_meta,
                   load_vault, read_notes, search_notes, snapshot)
from .staging import note_text

MAX_BACKUP_BYTES = 128 * 1024 * 1024
GUARD = '.km-source-status.json'
CONTROL_FILES = {GUARD, '.km-sync-selections.json'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def peak_rss(resource_module=resource):
    """Best-effort native peak RSS; unavailable where Python has no resource module."""
    if resource_module is None:
        return None
    return resource_module.getrusage(resource_module.RUSAGE_SELF).ru_maxrss


def environment():
    rss = peak_rss()
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'machine': platform.machine(), 'cpu': platform.processor(), 'logical_cpus': os.cpu_count(),
            'sqlite': sqlite3.sqlite_version, 'package': importlib.metadata.version('knowledge-memory'),
            'ram_bytes': os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') if hasattr(os, 'sysconf') else None,
            'peak_rss_native': rss,
            'peak_rss_unit': ('bytes' if sys.platform == 'darwin' else 'KiB') if rss is not None else None,
            'api_calls': 0, 'api_cost': 0, 'electricity_cost': None,
            'mcp_connected': False, 'cloud_auto_connected': False}


def backup(vault: Path, archive: Path):
    if archive.exists() or archive.is_symlink() or archive.resolve().is_relative_to(vault.resolve()):
        raise MemoryError('select a new archive outside vault')
    load_vault(vault)
    files = snapshot(vault)
    for name in CONTROL_FILES:
        control = vault / name
        if control.is_symlink():
            raise MemoryError('sync control symlink is forbidden')
        if control.exists():
            files[name] = control.read_bytes()
    if sum(map(len, files.values())) > MAX_BACKUP_BYTES:
        raise MemoryError('backup exceeds size limit')
    manifest = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                'files': {name: {'sha256': sha(raw), 'bytes': len(raw)} for name, raw in files.items()},
                'environment': environment()}
    archive.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=archive.parent, suffix='.zip')
    os.close(handle)
    try:
        with zipfile.ZipFile(name, 'w', compression=zipfile.ZIP_DEFLATED) as out:
            out.writestr('manifest.json', json.dumps(manifest, sort_keys=True))
            for path, raw in sorted(files.items()):
                out.writestr('vault/' + path, raw)
        # Hardlink makes publication exclusive, even if another process created archive.
        os.link(name, archive)
    finally:
        Path(name).unlink(missing_ok=True)
    return {'records': len([p for p in files if p.endswith('.md')]), 'files': len(files),
            'archive_sha256': sha(archive.read_bytes()), 'manifest': manifest}


def restore(archive: Path, target: Path):
    if target.exists() or target.is_symlink():
        raise MemoryError('restore target must not exist; never restore over current vault')
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) != len({entry.filename for entry in entries}):
            raise MemoryError('duplicate archive members')
        if sum(entry.file_size for entry in entries) > MAX_BACKUP_BYTES or len(entries) > 100000:
            raise MemoryError('archive exceeds limits')
        if any(entry.file_size > MAX_NOTE_BYTES for entry in entries if entry.filename != 'manifest.json'):
            raise MemoryError('archive member exceeds note limit')
        if source.getinfo('manifest.json').file_size > MAX_NOTE_BYTES:
            raise MemoryError('manifest exceeds size limit')
        manifest = json.loads(source.read('manifest.json'))
        if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
            raise MemoryError('invalid manifest')
        expected = {'manifest.json'} | {'vault/' + p for p in manifest['files']}
        if {e.filename for e in entries} != expected:
            raise MemoryError('archive and manifest disagree')
        raw_files = {}
        for path, info in manifest['files'].items():
            parts = PurePosixPath(path)
            if (not path or parts.is_absolute() or '..' in parts.parts or '\\' in path or ':' in path
                    or str(parts) != path or (path not in CONTROL_FILES and not path.endswith('.md'))):
                raise MemoryError('unsafe archive path')
            raw = source.read('vault/' + path)
            if len(raw) != info['bytes'] or sha(raw) != info['sha256']:
                raise MemoryError('archive checksum mismatch')
            raw_files[path] = raw
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target.parent, prefix='.km-restore-'))
    try:
        for path, raw in raw_files.items():
            out = staging / path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw)
        load_vault(staging)
        for name in CONTROL_FILES & raw_files.keys():
            if not isinstance(json.loads(raw_files[name]), dict):
                raise MemoryError('invalid sync control file')
        if target.exists():
            raise MemoryError('restore destination appeared; refusing replacement')
        os.rename(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {'files': len(raw_files), 'archive_sha256': sha(archive.read_bytes())}


def recovery_check(vault: Path, scope: str, queries: list[str], include_candidates=False):
    # Temporary material is deliberately outside the user's vault and is removed on exit.
    with tempfile.TemporaryDirectory(prefix='km-recovery-') as temporary:
        root = Path(temporary)
        archive, recovered = root / 'snapshot.zip', root / 'restored'
        saved = backup(vault, archive)
        restore(archive, recovered)
        before_db, after_db = root / 'before.sqlite3', root / 'after.sqlite3'
        build_index(vault, before_db)
        build_index(recovered, after_db)
        rows = []
        for query in queries:
            first = search_notes(before_db, query, scope, include_candidates=include_candidates)
            second = search_notes(after_db, query, scope, include_candidates=include_candidates)
            key = lambda values: [(r['id'], r['sha256'], r['metadata']) for r in values]
            rows.append({'query': query, 'same_results': key(first) == key(second),
                         'result_ids': [r['id'] for r in first]})
        return {'archive_sha256': saved['archive_sha256'], 'records': saved['records'],
                'same_bytes': snapshot(vault) == snapshot(recovered), 'queries': rows,
                'include_candidates': include_candidates,
                'environment': environment()}


def benchmark(sizes=(25, 250, 1000), repeats=5):
    if not sizes or any(type(n) is not int or not 1 <= n <= 10000 for n in sizes) or not 1 <= repeats <= 20:
        raise MemoryError('invalid benchmark size/repeats')
    results = []
    with tempfile.TemporaryDirectory(prefix='km-bench-') as temporary:
        root = Path(temporary)
        for count in sizes:
            vault = root / ('vault-' + str(count))
            vault.mkdir()
            for number in range(count):
                meta = dict(id=f'n-{number:05d}', title=f'架空記録{number}', kind='note', status='active',
                            scope='demo', claim_kind='source_report', updated='2026-09-26',
                            sources=[{'ref': 'fixture:benchmark', 'locator': str(number)}])
                (vault / (meta['id'] + '.md')).write_text(note_text(meta, '温度条件を固定して比較した。' * 20))
            db = root / (str(count) + '.sqlite3')
            started = time.perf_counter()
            build_index(vault, db)
            build_seconds = time.perf_counter() - started
            hashes, searches = [], []
            for _ in range(repeats):
                started = time.perf_counter()
                {path: sha(raw) for path, raw in snapshot(vault).items()}
                hashes.append(time.perf_counter() - started)
                started = time.perf_counter()
                search_notes(db, '温度', 'demo')
                searches.append(time.perf_counter() - started)
            results.append({'records': count, 'bytes': sum(p.stat().st_size for p in vault.glob('*.md')),
                            'index_seconds': build_seconds, 'hash_seconds': hashes, 'search_seconds': searches,
                            'hash_median_seconds': statistics.median(hashes),
                            'search_median_seconds': statistics.median(searches)})
    return {'environment': environment(), 'repeats': repeats, 'synthetic': True, 'rows': results,
            'decision': 'retain full hashing until target-machine latency/size demonstrates need for a watcher'}


async def probe_mcp(vault: Path, database: Path, scope: str, query: str, include_candidates=False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    parameters = StdioServerParameters(command=sys.executable, args=[
        '-m', 'knowledge_memory.mcp_server', '--vault', str(vault.resolve()),
        '--db', str(database.resolve()), '--scope', scope] +
        (['--include-candidates'] if include_candidates else []))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            found = await session.call_tool('search_memory', {'query': query})
            if found.isError:
                raise MemoryError('MCP search failed')
            hits = json.loads(found.content[0].text)
            ids = []
            for hit in hits[:3]:
                raw = await session.call_tool('get_memory', {'identifier': hit['id']})
                if raw.isError or json.loads(raw.content[0].text)['sha256'] != hit['sha256']:
                    raise MemoryError('MCP source mismatch')
                ids.append(hit['id'])
    return {'mcp_connected': True, 'cloud_auto_connected': False, 'llm_initiated': False,
            'tools': [tool.name for tool in tools.tools], 'source_ids': ids, 'client': 'official SDK stdio',
            'include_candidates': include_candidates, 'retrieval_verified': bool(ids)}


def measure(vault: Path, database: Path, scope: str, queries: list[str],
            repeats=5, include_candidates=False):
    """Time an existing index without rebuilding it or changing its source records."""
    if type(repeats) is not int or not 1 <= repeats <= 20:
        raise MemoryError('repeats must be between 1 and 20')
    if (not isinstance(queries, list) or not 1 <= len(queries) <= 50
            or any(not isinstance(q, str) or not q.strip() or len(q) > 512 for q in queries)):
        raise MemoryError('provide 1 to 50 nonempty queries of at most 512 characters')
    meta = index_meta(database)  # Includes normal stale-index and source-ledger checks.
    if Path(meta['vault']).resolve() != vault.resolve():
        raise MemoryError('database belongs to another vault')
    def state():
        files = snapshot(vault)
        for name in CONTROL_FILES:
            path = vault / name
            if path.is_symlink():
                raise MemoryError('sync control symlink is forbidden')
            if path.exists():
                files[name] = path.read_bytes()
        return {name: sha(raw) for name, raw in files.items()}

    initial = state()
    eligible = len(read_notes(database, scope, include_candidates))
    sizes = snapshot(vault)
    records, total_bytes = len(sizes), sum(map(len, sizes.values()))
    del sizes
    for query in queries:  # Warm the normal retrieval path; exclude warmup from timing.
        search_notes(database, query, scope, include_candidates=include_candidates)
    hashes = []
    rows = [{'query_sha256': sha(q.encode()), 'search_seconds': [], 'hit_counts': []} for q in queries]
    for _ in range(repeats):
        started = time.perf_counter()
        current = state()
        hashes.append(time.perf_counter() - started)
        if current != initial:
            raise MemoryError('vault changed during measurement; stop writers and retry')
        for query, row in zip(queries, rows):
            started = time.perf_counter()
            found = search_notes(database, query, scope, include_candidates=include_candidates)
            row['search_seconds'].append(time.perf_counter() - started)
            row['hit_counts'].append(len(found))
    if index_meta(database) != meta or state() != initial:
        raise MemoryError('vault or index changed during measurement; retry')
    for row in rows:
        row['search_median_seconds'] = statistics.median(row['search_seconds'])
    return {'created_at': datetime.now(timezone.utc).isoformat(), 'environment': environment(),
            'corpus': 'operator-selected existing vault', 'scope': scope,
            'include_candidates': include_candidates, 'repeats': repeats, 'warmup_per_query': 1,
            'records': records, 'eligible_records': eligible, 'bytes': total_bytes,
            'vault_fingerprint': sha(json.dumps(initial, sort_keys=True).encode()),
            'index_schema': meta['schema_version'], 'hash_seconds': hashes,
            'hash_median_seconds': statistics.median(hashes), 'queries': rows,
            'index_rebuilt': False, 'vault_modified': False}


def time_comparison(path: Path):
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or not rows:
        raise MemoryError('supply observed matched tasks')
    for row in rows:
        for key in ('reexplain_seconds', 'organize_seconds', 'retrieve_seconds'):
            if type(row.get(key)) not in (int, float) or not 0 <= row[key] < 1e8:
                raise MemoryError('invalid observed duration')
    baseline = sum(r['reexplain_seconds'] for r in rows)
    assisted = sum(r['organize_seconds'] + r['retrieve_seconds'] for r in rows)
    return {'tasks': len(rows), 'reexplain_seconds': baseline, 'organize_plus_retrieve_seconds': assisted,
            'net_seconds_saved': baseline - assisted, 'source': 'operator-entered observations, not automatically verified'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['backup', 'restore', 'recovery', 'benchmark', 'measure', 'probe-mcp', 'time-compare'])
    p.add_argument('--vault', type=Path)
    p.add_argument('--archive', type=Path)
    p.add_argument('--target', type=Path)
    p.add_argument('--db', type=Path)
    p.add_argument('--scope')
    p.add_argument('--include-candidates', action='store_true')
    p.add_argument('--query', action='append', default=[])
    p.add_argument('--observations', type=Path)
    p.add_argument('--repeats', type=int, default=5)
    a = p.parse_args()
    try:
        if a.action == 'benchmark':
            result = benchmark(repeats=a.repeats)
        elif a.action == 'measure':
            result = measure(a.vault, a.db, a.scope, a.query, a.repeats, a.include_candidates)
        elif a.action == 'time-compare':
            result = time_comparison(a.observations)
        elif a.action == 'backup':
            result = backup(a.vault, a.archive)
        elif a.action == 'restore':
            result = restore(a.archive, a.target)
        elif a.action == 'recovery':
            if not a.query:
                raise MemoryError('provide at least one query')
            result = recovery_check(a.vault, a.scope, a.query, a.include_candidates)
        else:
            if len(a.query) != 1:
                raise MemoryError('provide exactly one query')
            result = asyncio.run(asyncio.wait_for(probe_mcp(a.vault, a.db, a.scope, a.query[0], a.include_candidates), 30))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MemoryError, OSError, ValueError, AttributeError, KeyError, zipfile.BadZipFile):
        print('{"error":"operation refused; check paths, manifest and inputs"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

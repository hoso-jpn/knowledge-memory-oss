from datetime import datetime, timedelta, timezone
import base64
import errno
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
from knowledge_memory.core import MemoryError, build_index, get_note, search_notes
from knowledge_memory.mcp_server import BoundedMemoryTools
from knowledge_memory.runtime import (STATUS, SerializedTools, manifest, read_status,
                                      reconcile, release_retry, run, runtime_lock)
from knowledge_memory.sync import GUARD, SELECTIONS, FetchError
from test_data_source_sync import FakeAPI, OTHER_DS
from test_knowledge_import import CONFIG, DS_ID


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.vault = self.root / 'vault'
        self.db = self.root / 'index.sqlite3'; self.state = self.root / 'state'
        self.config = self.root / 'sync.yaml'; self.api = FakeAPI()
        (self.root / 'import.yaml').write_text(yaml.safe_dump(CONFIG))
        self.config.write_text(yaml.safe_dump({'selection_id': 'fixture', 'scope': 'demo',
            'sources': [{'provider': 'notion', 'data_source_id': DS_ID, 'import_config': 'import.yaml'}]}))

    def invoke(self, **kwargs):
        return run(self.config, self.vault, self.db, self.state, fetch=self.api, **kwargs)

    def test_success_manifest_and_readable_status(self):
        first = self.invoke()
        self.assertEqual(first['phase'], 'ready'); self.assertEqual(first['indexed_records'], 2)
        self.assertIn('last_complete_enumeration', first)
        self.assertEqual(first['index_sha256'], hashlib.sha256(self.db.read_bytes()).hexdigest())
        second = self.invoke()
        self.assertEqual(second['source_counts'], {'unchanged': 2})
        self.assertEqual(second['consecutive_failures'], 0)
        self.assertEqual(second, read_status(self.state))
        evidence = manifest(self.config, 'a' * 40)
        self.assertFalse(evidence['commit_verified'])
        self.assertIn('mcp_server.py', evidence['source_files_sha256'])
        original = evidence['config_sha256']
        changed = dict(CONFIG); changed['properties'] = dict(CONFIG['properties'], title='別の題名')
        (self.root / 'import.yaml').write_text(yaml.safe_dump(changed))
        self.assertNotEqual(manifest(self.config)['config_sha256'], original)

    def test_lock_blocks_duplicate_writer_and_mcp_and_is_released(self):
        self.invoke(); calls = len(self.api.calls)
        tools = SerializedTools(BoundedMemoryTools(self.vault, self.db, 'demo', True))
        with runtime_lock(self.vault):
            with self.assertRaisesRegex(MemoryError, 'busy'): self.invoke()
            with self.assertRaisesRegex(MemoryError, 'busy'):
                tools.call('search_memory_bounded', {'query': '本文'})
        self.assertEqual(calls, len(self.api.calls))
        self.assertTrue(tools.call('search_memory_bounded', {'query': '本文'})['records'])
        self.assertTrue((self.vault / '.km-runtime.lock').exists())

    def test_lock_is_shared_across_processes(self):
        self.invoke()
        code = 'from pathlib import Path; from knowledge_memory.runtime import runtime_lock; import sys\nwith runtime_lock(Path(sys.argv[1])): pass'
        with runtime_lock(self.vault):
            result = subprocess.run([sys.executable, '-c', code, str(self.vault)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'busy', result.stderr)
        result = subprocess.run([sys.executable, '-c', code, str(self.vault)], capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_access_failure_keeps_originals_and_old_index_cannot_bypass_ledger(self):
        good = self.invoke(); db = self.db.read_bytes()
        notes = {p.name: p.read_bytes() for p in self.vault.rglob('*.md')}
        self.api.fail['/data_sources/' + DS_ID] = FetchError('inaccessible')
        failed = self.invoke()
        self.assertEqual(failed['phase'], 'failed')
        self.assertEqual(failed['last_success'], good['last_success'])
        self.assertEqual(failed['consecutive_failures'], 1)
        self.assertEqual(failed['enumeration_counts'], {'inaccessible': 1})
        self.assertEqual(db, self.db.read_bytes())
        self.assertEqual(notes, {p.name: p.read_bytes() for p in self.vault.rglob('*.md')})
        self.assertEqual(search_notes(self.db, '本文', 'demo', include_candidates=True), [])
        with self.assertRaises(MemoryError): get_note(self.db, 'src-' + f'{1:032x}', 'demo', True)
        calls = len(self.api.calls)
        with self.assertRaisesRegex(MemoryError, 'backoff'): self.invoke()
        self.assertEqual(calls, len(self.api.calls))
        self.api.fail.clear()
        with patch('knowledge_memory.runtime.now', return_value=datetime.fromisoformat(failed['next_retry_at']) + timedelta(seconds=1)):
            self.assertEqual(self.invoke()['phase'], 'ready')

    def test_unknown_retry_after_blocks_timer_until_operator_records_deadline(self):
        self.api.fail['/data_sources/' + DS_ID] = FetchError('retry_later')
        result = self.invoke()
        self.assertTrue(result['retry_deferred']); self.assertIsNone(result['next_retry_at'])
        with self.assertRaisesRegex(MemoryError, 'deadline unknown'): self.invoke()
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        release_retry(self.vault, self.state, future.isoformat())
        with self.assertRaisesRegex(MemoryError, 'backoff'): self.invoke()
        self.assertIn('operator', read_status(self.state)['retry_deadline_source'])
        self.assertGreater(result['logical_api_calls'], 0)

    def test_index_failure_and_interrupted_attempt_are_visible(self):
        def broken(*args): raise OSError('synthetic index failure')
        failed = self.invoke(indexer=broken)
        self.assertEqual(failed['failure'], 'index_failed'); self.assertNotIn('last_success', failed)
        failed.update(phase='index', next_retry_at=None)
        (self.state / STATUS).write_text(json.dumps(failed))
        ready = self.invoke()
        self.assertTrue(ready['previous_attempt_interrupted'])
        self.assertEqual(ready['phase'], 'ready')

    def test_missing_member_failure_blocks_ready_and_immediate_retry(self):
        good = self.invoke(); original = self.db.read_bytes()
        missing = self.api.members[DS_ID].pop()
        for failure in ('fetch_failed', 'inaccessible', 'missing_or_inaccessible', 'retry_later'):
            with self.subTest(failure=failure):
                # Independently start each failure from the last known ready state.
                (self.state / STATUS).write_text(json.dumps(good))
                self.api.fail['/pages/' + missing] = FetchError(failure)
                result = self.invoke()
                self.assertEqual(result['phase'], 'failed')
                self.assertEqual(result['source_counts'][failure], 1)
                self.assertEqual(result['last_success'], good['last_success'])
                self.assertEqual(result['retry_deferred'], failure == 'retry_later')
                self.assertEqual(self.db.read_bytes(), original)
                calls = len(self.api.calls)
                with self.assertRaises(MemoryError): self.invoke()
                self.assertEqual(len(self.api.calls), calls)
                with self.assertRaises(MemoryError):
                    get_note(self.db, 'src-' + missing, 'demo', True)

    def test_verified_archived_member_is_held_after_successful_index(self):
        self.invoke()
        missing = self.api.members[DS_ID].pop()
        self.api.records[missing]['page']['archived'] = True
        result = self.invoke()
        self.assertEqual(result['phase'], 'ready')
        self.assertEqual(result['source_counts']['archived'], 1)
        with self.assertRaises(MemoryError): get_note(self.db, 'src-' + missing, 'demo', True)

    def test_import_mapping_cannot_be_index_output_or_inside_managed_paths(self):
        for mapping in (self.db, self.vault / 'mapping.yaml', self.state / 'mapping.yaml'):
            with self.subTest(mapping=mapping.name):
                mapping.parent.mkdir(parents=True, exist_ok=True)
                mapping.write_text(yaml.safe_dump(CONFIG))
                original = mapping.read_bytes()
                config = yaml.safe_load(self.config.read_text())
                config['sources'][0]['import_config'] = str(mapping)
                self.config.write_text(yaml.safe_dump(config))
                with self.assertRaisesRegex(MemoryError, 'safely separated'): self.invoke()
                self.assertEqual(mapping.read_bytes(), original)
                self.assertEqual(self.api.calls, [])
                self.assertFalse((self.state / STATUS).exists())
                self.assertFalse((self.vault / '.km-runtime.lock').exists())

    def test_foreign_state_and_unsafe_paths_are_refused(self):
        self.invoke()
        other = self.root / 'other'; other.mkdir()
        with self.assertRaisesRegex(MemoryError, 'another profile'):
            run(self.config, other, self.db, self.state, fetch=self.api)
        with self.assertRaises(MemoryError):
            run(self.config, self.vault, self.vault / 'bad.sqlite3', self.state, fetch=self.api)
        alias = self.root / 'alias.sqlite3'; alias.symlink_to(self.db)
        with self.assertRaises(MemoryError): run(self.config, self.vault, alias, self.state, fetch=self.api)

    def test_incomplete_pagination_is_not_a_successful_generation(self):
        self.invoke(); self.api.bad_cursor = True
        result = self.invoke()
        self.assertEqual(result['failure'], 'sync_incomplete')
        self.assertEqual(result['source_counts'], {})
        self.assertEqual(search_notes(self.db, '本文', 'demo', include_candidates=True), [])

    def select_pages(self, pages):
        self.config.write_text(yaml.safe_dump({'selection_id': 'fixture', 'scope': 'demo',
            'sources': [{'provider': 'notion', 'page_id': page, 'import_config': 'import.yaml'}
                        for page in pages]}))

    def restored_copy(self, name='restored'):
        return Path(shutil.copytree(self.vault, self.root / name))

    def reconcile_cli(self, restored, reference=True):
        command = [sys.executable, '-m', 'knowledge_memory.runtime', 'reconcile',
                   '--config', str(self.config), '--vault', str(restored)]
        if reference:
            command += ['--current-vault', str(self.vault)]
        return subprocess.run(command, capture_output=True, text=True)

    def test_reconcile_reports_a_restored_source_that_is_no_longer_selected(self):
        pages = [f'{1:032x}', f'{2:032x}']
        self.select_pages(pages)
        self.assertEqual(self.invoke()['phase'], 'ready')
        restored = self.restored_copy()
        self.assertTrue(reconcile(self.config, restored, self.vault)['reconciled'])

        self.select_pages(pages[:1])
        self.assertEqual(self.invoke()['phase'], 'ready')
        self.assertFalse(search_notes(self.db, '架空資料2', 'demo', include_candidates=True))
        stale = reconcile(self.config, restored, self.vault)
        self.assertFalse(stale['reconciled'])
        self.assertEqual([row['source'] for row in stale['would_be_served_but_unselected']],
                         ['notion:' + pages[1]])
        self.assertEqual(stale['would_be_served_but_unselected'][0]['status'], 'current')
        self.assertEqual(self.reconcile_cli(restored).returncode, 2)

        current_copy = self.restored_copy('held-copy')
        current = reconcile(self.config, current_copy, self.vault)
        self.assertTrue(current['reconciled'])
        self.assertEqual([row['source'] for row in current['held_in_ledger']], ['notion:' + pages[1]])

    def test_reconcile_rejects_a_different_data_source(self):
        self.invoke()
        restored = self.restored_copy()
        config = yaml.safe_load(self.config.read_text())
        config['sources'][0]['data_source_id'] = OTHER_DS
        self.config.write_text(yaml.safe_dump(config))
        current = self.root / 'new-vault'
        result = run(self.config, current, self.root / 'new.sqlite3', self.root / 'new-state', fetch=self.api)
        self.assertEqual(result['phase'], 'ready')
        result = reconcile(self.config, restored, current)
        self.assertTrue(result['current_registry_matches_config'])
        self.assertFalse(result['reconciled'])
        self.assertEqual(len(result['would_be_served_but_unselected']), 2)

    def test_reconcile_preserves_holds_even_when_allowlist_did_not_change(self):
        self.invoke()
        restored = self.restored_copy()
        self.api.fail['/data_sources/' + DS_ID] = FetchError('inaccessible')
        self.assertEqual(self.invoke()['phase'], 'failed')
        self.assertEqual(search_notes(self.db, '本文', 'demo', include_candidates=True), [])
        restored_db = self.root / 'restored.sqlite3'
        build_index(restored, restored_db)
        self.assertEqual(len(search_notes(restored_db, '本文', 'demo', include_candidates=True)), 2)
        result = reconcile(self.config, restored, self.vault)
        self.assertFalse(result['reconciled'])
        self.assertEqual(result['would_be_served_but_unselected'], [])
        self.assertEqual([row['current_status'] for row in result['would_be_served_but_held_now']],
                         ['inaccessible', 'inaccessible'])
        self.assertEqual(self.reconcile_cli(restored).returncode, 2)

    def test_reconcile_missing_current_source_cannot_authorize_old_content(self):
        self.invoke()
        restored = self.restored_copy()
        ledger = json.loads((self.vault / GUARD).read_text())
        key, entry = ledger.popitem()
        (self.vault / GUARD).write_text(json.dumps(ledger))
        (self.vault / '20-sources' / (entry['note_id'] + '.md')).unlink()
        result = reconcile(self.config, restored, self.vault)
        self.assertFalse(result['reconciled'])
        self.assertEqual(result['would_be_served_but_held_now'][0]['source'], key)
        self.assertEqual(result['would_be_served_but_held_now'][0]['current_status'], 'missing')

    def test_reconcile_flags_foreign_ownership_and_registry_mismatch(self):
        self.invoke()
        restored = self.restored_copy()
        config = yaml.safe_load(self.config.read_text())
        config['selection_id'] = 'elsewhere'
        self.config.write_text(yaml.safe_dump(config))
        result = reconcile(self.config, restored, self.vault)
        self.assertFalse(result['reconciled'])
        self.assertFalse(result['restored_registry_matches_profile'])
        self.assertFalse(result['current_registry_matches_config'])
        self.assertEqual(len(result['owned_by_another_selection']), 4)
        config['selection_id'] = 'fixture'
        self.config.write_text(yaml.safe_dump(config))
        registry = json.loads((self.vault / SELECTIONS).read_text())
        registry['fixture']['containers'] = ['notion-data-source:' + OTHER_DS]
        (self.vault / SELECTIONS).write_text(json.dumps(registry))
        result = reconcile(self.config, restored, self.vault)
        self.assertFalse(result['reconciled'])
        self.assertFalse(result['current_registry_matches_config'])

    def test_reconcile_rejects_empty_partial_and_malformed_ledgers_on_either_side(self):
        self.invoke()
        restored = self.restored_copy()
        original = json.loads((self.vault / GUARD).read_text())
        key = next(iter(original))
        corruptions = [
            {}, {key: original[key]},
            dict(original, **{key: dict(original[key], note_id='src-wrong')}),
            dict(original, **{key: dict(original[key], status=None)}),
            dict(original, **{key: dict(original[key], data_source_id=123)}),
        ]
        for vault in (restored, self.vault):
            for broken in corruptions:
                with self.subTest(side=vault.name, keys=len(broken)):
                    (vault / GUARD).write_text(json.dumps(broken))
                    with self.assertRaises(MemoryError): reconcile(self.config, restored, self.vault)
                    (vault / GUARD).write_text(json.dumps(original))
            duplicate = json.dumps(original)[:-1] + ', ' + json.dumps(key) + ': ' + json.dumps(original[key]) + '}'
            (vault / GUARD).write_text(duplicate)
            with self.assertRaisesRegex(MemoryError, 'duplicate'): reconcile(self.config, restored, self.vault)
            (vault / GUARD).write_text(json.dumps(original))

    def test_reconcile_requires_independent_reference_and_regular_metadata(self):
        self.invoke()
        restored = self.restored_copy()
        self.assertEqual(self.reconcile_cli(restored, reference=False).returncode, 2)
        alias = self.root / 'alias'
        alias.symlink_to(self.vault, target_is_directory=True)
        for target in (self.vault, alias, self.vault / 'nested'):
            with self.assertRaisesRegex(MemoryError, 'independent'):
                reconcile(self.config, target, self.vault)
        for name in (GUARD, SELECTIONS):
            path = restored / name
            original = path.read_bytes()
            path.unlink()
            with self.assertRaisesRegex(MemoryError, 'regular'): reconcile(self.config, restored, self.vault)
            path.symlink_to(self.vault / name)
            with self.assertRaises(MemoryError): reconcile(self.config, restored, self.vault)
            path.unlink()
            path.hardlink_to(self.vault / name)
            with self.assertRaisesRegex(MemoryError, 'independent'): reconcile(self.config, restored, self.vault)
            path.unlink()
            path.write_bytes(original)

    def test_reconcile_does_not_restore_old_note_status(self):
        self.invoke()
        restored = self.restored_copy()
        note = next(self.vault.rglob('*.md'))
        note.write_text(note.read_text().replace('status: candidate', 'status: retracted'))
        result = reconcile(self.config, restored, self.vault)
        self.assertFalse(result['reconciled'])
        self.assertEqual(len(result['would_restore_different_note_status']), 1)

    def test_reconcile_checks_note_identity_scope_and_missing_current_note(self):
        self.invoke()
        restored = self.restored_copy()
        note = next(restored.rglob('*.md'))
        original = note.read_bytes()
        for before, after in [('scope: demo', 'scope: another'),
                              ('record_id: ', 'record_id: bad')]:
            note.write_text(original.decode().replace(before, after, 1))
            with self.assertRaises(MemoryError): reconcile(self.config, restored, self.vault)
        note.unlink()
        with self.assertRaisesRegex(MemoryError, 'corresponding note'):
            reconcile(self.config, restored, self.vault)

    def test_reconcile_accepts_matching_data_source_without_network_or_writes(self):
        self.assertEqual(self.invoke()['phase'], 'ready')
        restored = self.restored_copy()
        def files():
            return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.root.rglob('*') if p.is_file()}
        before = files()
        calls = len(self.api.calls)
        with patch('knowledge_memory.runtime.request', side_effect=AssertionError('unexpected network')):
            result = reconcile(self.config, restored, self.vault)
        self.assertTrue(result['reconciled'])
        self.assertEqual(result['ledger_entries'], 2)
        self.assertEqual(self.reconcile_cli(restored).returncode, 0)
        self.assertEqual(files(), before)
        self.assertEqual(len(self.api.calls), calls)

    def test_reconcile_supports_legacy_github_managed_vault(self):
        self.config.write_text(yaml.safe_dump({'scope': 'demo', 'sources': [
            {'provider': 'github', 'repo': 'example/demo', 'path': 'guide.md', 'ref': 'main'}]}))
        def fetch(*args, **kwargs):
            return {'type': 'file', 'encoding': 'base64',
                    'content': base64.b64encode(b'original evidence').decode(), 'sha': 'a' * 40}
        result = run(self.config, self.vault, self.db, self.state, fetch=fetch)
        self.assertEqual(result['phase'], 'ready')
        restored = self.restored_copy()
        result = reconcile(self.config, restored, self.vault)
        self.assertTrue(result['reconciled'])
        self.assertEqual(result['config_selection_id'], 'legacy')

    def test_serialized_reader_participates_from_a_read_only_vault(self):
        """A read-only bind of the writer's vault shares its actual lock inode."""
        self.invoke()
        lock = self.vault / '.km-runtime.lock'
        tools = SerializedTools(BoundedMemoryTools(self.vault, self.db, 'demo', True))
        real_open = os.open
        original_inode = lock.stat().st_ino
        for failure in (errno.EROFS, errno.EACCES, errno.EPERM):
            def read_only(path, flags, *args):
                if str(path).endswith('.km-runtime.lock') and flags & os.O_RDWR:
                    raise OSError(failure, 'synthetic write rejection', str(path))
                return real_open(path, flags, *args)

            # Acquire the writer through a real O_RDWR descriptor before mocking
            # the reader's mount/permissions; do not turn both into O_RDONLY.
            with runtime_lock(self.vault):
                with self.subTest(errno=failure), patch('knowledge_memory.runtime.os.open', read_only):
                    with self.assertRaisesRegex(MemoryError, 'busy'):
                        tools.call('search_memory_bounded', {'query': '本文'})
            with patch('knowledge_memory.runtime.os.open', read_only):
                self.assertTrue(tools.call('search_memory_bounded', {'query': '本文'})['records'])
        self.assertEqual(lock.stat().st_ino, original_inode)

    def test_read_only_holder_excludes_a_writer_in_another_process(self):
        self.invoke()
        real_open = os.open

        def read_only(path, flags, *args):
            if str(path).endswith('.km-runtime.lock') and flags & os.O_RDWR:
                raise OSError(errno.EROFS, 'synthetic read-only mount')
            return real_open(path, flags, *args)

        code = ('from pathlib import Path\nfrom knowledge_memory.runtime import runtime_lock\n'
                'import sys\nwith runtime_lock(Path(sys.argv[1])): pass')
        with patch('knowledge_memory.runtime.os.open', read_only), runtime_lock(self.vault):
            result = subprocess.run([sys.executable, '-c', code, str(self.vault)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'busy', result.stderr)
        result = subprocess.run([sys.executable, '-c', code, str(self.vault)], capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_serialized_refuses_a_read_only_vault_without_the_lock_inode(self):
        """Missing the inode must fail loudly, never run unserialized."""
        self.invoke()
        # No process is holding this test vault's lock. Actually remove it so
        # the fallback O_RDONLY open encounters ENOENT, not a second EROFS mock.
        (self.vault / '.km-runtime.lock').unlink()
        tools = SerializedTools(BoundedMemoryTools(self.vault, self.db, 'demo', True))
        real_open = os.open

        def read_only(path, flags, *args):
            if str(path).endswith('.km-runtime.lock') and flags & os.O_RDWR:
                raise OSError(errno.EROFS, 'Read-only file system', str(path))
            return real_open(path, flags, *args)

        with patch('knowledge_memory.runtime.os.open', read_only):
            with self.assertRaisesRegex(MemoryError, 'km-runtime'):
                tools.call('search_memory_bounded', {'query': '本文'})
            with self.assertRaisesRegex(MemoryError, 'km-runtime'):
                self.invoke()
        self.assertFalse((self.vault / '.km-runtime.lock').exists())

    def test_read_only_fallback_does_not_follow_a_lock_symlink(self):
        self.invoke()
        lock = self.vault / '.km-runtime.lock'
        lock.unlink()  # No active holder in this isolated fixture.
        target = self.root / 'other-lock'
        target.write_bytes(b'preserve this file')
        lock.symlink_to(target)
        real_open = os.open

        def read_only(path, flags, *args):
            if str(path).endswith('.km-runtime.lock') and flags & os.O_RDWR:
                raise OSError(errno.EROFS, 'synthetic read-only mount')
            return real_open(path, flags, *args)

        with patch('knowledge_memory.runtime.os.open', read_only):
            with self.assertRaisesRegex(MemoryError, 'km-runtime'):
                with runtime_lock(self.vault): pass
        self.assertTrue(lock.is_symlink())
        self.assertEqual(target.read_bytes(), b'preserve this file')

    def test_lock_errors_other_than_read_only_are_not_masked(self):
        self.invoke()
        real_open = os.open

        def broken(path, flags, *args):
            if str(path).endswith('.km-runtime.lock'):
                raise OSError(errno.ENAMETOOLONG, 'File name too long', str(path))
            return real_open(path, flags, *args)

        with patch('knowledge_memory.runtime.os.open', broken):
            with self.assertRaises(OSError) as caught:
                with runtime_lock(self.vault):
                    pass
            self.assertEqual(caught.exception.errno, errno.ENAMETOOLONG)

    def test_lock_file_is_excluded_from_the_indexed_vault_snapshot(self):
        """Pre-creating the lock must not make an already built index look stale."""
        first = self.invoke()
        (self.vault / '.km-runtime.lock').touch()
        # The reader rechecks every indexed path against the vault on each call.
        tools = BoundedMemoryTools(self.vault, self.db, 'demo', True)
        self.assertTrue(tools.call('search_memory_bounded', {'query': '本文'})['records'])
        again = self.invoke()
        self.assertEqual(again['phase'], 'ready')
        self.assertEqual(again['indexed_records'], first['indexed_records'])

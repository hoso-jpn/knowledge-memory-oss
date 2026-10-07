import copy
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from knowledge_memory.core import MemoryError, build_index, search_notes
from knowledge_memory.operations import backup, restore
from knowledge_memory.retention import PINS, apply_retention, main, plan_retention
from knowledge_memory.runtime import reconcile, run, runtime_lock
from knowledge_memory.sync import GUARD, SELECTIONS, sync
from knowledge_memory.sync import encoded
from test_data_source_sync import FakeAPI
from test_knowledge_import import CONFIG, DS_ID, rt


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault, self.state, self.db = (self.root / name for name in ('vault', 'state', 'db.sqlite3'))
        self.config = self.root / 'sync.yaml'
        (self.root / 'import.yaml').write_text(yaml.safe_dump(CONFIG))
        self.config.write_text(yaml.safe_dump({'selection_id': 'fixture', 'scope': 'demo', 'sources': [
            {'provider': 'notion', 'data_source_id': DS_ID, 'import_config': 'import.yaml'}]}))
        self.api = FakeAPI()
        self.previous_files = set()
        self.generation = 0
        self.old_time = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()

    def synchronize(self):
        self.generation += 1
        for record in self.api.records.values():
            record['page']['request_id'] = f'synthetic-request-{self.generation}'
        result = run(self.config, self.vault, self.db, self.state, fetch=self.api)
        files = {p for folder in ('evidence', 'enumerations') for p in (self.state / folder).glob('*.json')}
        for path in files - self.previous_files:
            os.utime(path, (self.old_time + self.generation, self.old_time + self.generation))
        self.previous_files = files
        return result

    def populate(self, count=6):
        for _ in range(count):
            self.synchronize()

    def plan(self, **kwargs):
        return plan_retention(self.config, self.vault, self.state, keep_days=7, **kwargs)

    def apply(self, document):
        return apply_retention(document, document['plan']['plan_sha256'])

    def fingerprint(self):
        return {str(p.relative_to(self.root)): (hashlib.sha256(p.read_bytes()).hexdigest(),
                p.stat().st_size, p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}

    def test_unchanged_raw_requests_expire_but_note_and_latest_reads_survive(self):
        self.populate()
        self.assertEqual(len(list((self.state / 'evidence').glob('*.json'))), 12)
        before = {str(p): p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
        document = self.plan(keep_latest=2)
        self.assertEqual(document['plan']['summary']['evidence']['delete_files'], 6)
        self.assertEqual(document['plan']['summary']['enumerations']['delete_files'], 4)
        result = self.apply(document)
        self.assertTrue(result['completed'])
        self.assertEqual(len(result['removed']), 10)
        self.assertEqual(len(list((self.state / 'evidence').glob('*.json'))), 6)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.vault.rglob('*') if p.is_file()})
        self.assertTrue(search_notes(self.db, '本文', 'demo', include_candidates=True))
        for _ in range(3):
            self.assertEqual(self.synchronize()['source_counts'], {'unchanged': 2})
            self.apply(self.plan())
            self.assertEqual(len(list((self.state / 'evidence').glob('*.json'))), 6)
            self.assertEqual(len(list((self.state / 'enumerations').glob('*.json'))), 2)

    def test_plan_does_not_modify_content_size_file_or_directory_mtime(self):
        self.populate()
        before = self.fingerprint()
        directories = {str(p): p.stat().st_mtime_ns for p in self.root.rglob('*') if p.is_dir()}
        calls = len(self.api.calls)
        self.plan()
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(directories, {str(p): p.stat().st_mtime_ns for p in self.root.rglob('*') if p.is_dir()})
        self.assertEqual(len(self.api.calls), calls)

    def test_recent_evidence_is_kept_even_when_budget_is_exceeded(self):
        self.populate()
        for path in (self.state / 'evidence').iterdir():
            os.utime(path, None)
        document = self.plan(budget_bytes=1)
        self.assertEqual(document['plan']['summary']['evidence']['delete_files'], 0)
        self.assertTrue(document['plan']['budget_exceeded_after_plan'])
        self.assertTrue(self.apply(document)['budget_exceeded'])

    def test_explicit_pins_keep_exact_artifacts_and_reject_missing_or_wrong_hash(self):
        self.populate()
        candidate = next(r for r in self.plan()['plan']['artifacts'] if r['action'] == 'delete')
        path = self.state / PINS
        path.write_text(json.dumps({'version': 1, 'files': {candidate['path']: candidate['sha256']}}))
        document = self.plan()
        pinned = next(r for r in document['plan']['artifacts'] if r['path'] == candidate['path'])
        self.assertIn('explicit_pin', pinned['protected_by'])
        self.apply(document)
        self.assertTrue((self.state / candidate['path']).exists())
        path.write_text(json.dumps({'version': 1, 'files': {candidate['path']: '0' * 64}}))
        with self.assertRaisesRegex(MemoryError, 'pinned evidence'):
            self.plan()

    def test_backup_reference_survives_pruning_restore_and_reconcile(self):
        self.synchronize()
        archive = self.root / 'backup.zip'
        backup(self.vault, archive)
        restored = self.root / 'restored'
        restore(archive, restored)
        for record in self.api.records.values():
            record['blocks'][1]['paragraph']['rich_text'] = [rt('本文の自然な改訂')]
        self.populate(5)
        unpinned = self.plan(keep_latest=1)
        document = self.plan(keep_latest=1, protected_vaults=[restored])
        self.assertLess(document['plan']['summary']['evidence']['delete_files'], unpinned['plan']['summary']['evidence']['delete_files'])
        backup_before = archive.read_bytes()
        self.apply(document)
        self.assertEqual(archive.read_bytes(), backup_before)
        self.assertTrue(reconcile(self.config, restored, self.vault)['reconciled'])
        build_index(restored, self.root / 'restored.sqlite3')
        self.assertTrue(search_notes(self.root / 'restored.sqlite3', '本文', 'demo', include_candidates=True))
        self.plan(protected_vaults=[restored])  # All required raw hashes are still resolvable.

    def test_held_source_history_and_retrieval_guard_are_preserved(self):
        self.populate()
        path = self.vault / GUARD
        ledger = json.loads(path.read_text())
        for entry in ledger.values():
            entry['status'] = 'inaccessible'
        path.write_text(json.dumps(ledger))
        before = path.read_bytes()
        document = self.plan()
        self.assertEqual(document['plan']['summary']['evidence']['delete_files'], 0)
        self.apply(document)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(search_notes(self.db, '本文', 'demo', include_candidates=True), [])

    def test_interrupted_checkpoint_refuses_pruning_and_remains_resumable(self):
        self.populate()
        path = self.state / 'checkpoint.json'
        checkpoint = json.loads(path.read_text())
        checkpoint['complete'] = False
        path.write_text(json.dumps(checkpoint))
        before = self.fingerprint()
        with self.assertRaisesRegex(MemoryError, 'completed checkpoint'):
            self.plan()
        self.assertEqual(self.fingerprint(), before)
        self.assertTrue(sync(self.config, self.vault, self.state, resume=True, fetch=self.api)['complete'])

    def test_plan_and_apply_refuse_both_runtime_and_legacy_writers(self):
        self.populate()
        document = self.plan()
        with runtime_lock(self.vault):
            with self.assertRaisesRegex(MemoryError, 'busy'):
                self.plan()
            with self.assertRaisesRegex(MemoryError, 'busy'):
                self.apply(document)
        (self.vault / '.km-sync.lock').touch()
        with self.assertRaisesRegex(MemoryError, 'sync lock'):
            self.plan()
        with self.assertRaisesRegex(MemoryError, 'sync lock'):
            self.apply(document)

    def test_apply_holds_both_writer_locks_during_deletion(self):
        self.populate()
        document = self.plan()
        real_unlink = os.unlink
        checked = []

        def unlink(path, **kwargs):
            if 'dir_fd' in kwargs:
                with self.assertRaisesRegex(MemoryError, 'busy'):
                    with runtime_lock(self.vault):
                        pass
                self.assertTrue((self.vault / '.km-sync.lock').exists())
                checked.append(path)
            return real_unlink(path, **kwargs)

        with patch('knowledge_memory.retention.os.unlink', unlink):
            self.assertTrue(self.apply(document)['completed'])
        self.assertTrue(checked)

    def test_changed_sync_or_config_invalidates_saved_plan_before_deletion(self):
        self.populate()
        document = self.plan()
        self.synchronize()
        before = self.fingerprint()
        with self.assertRaisesRegex(MemoryError, 'stale or altered'):
            self.apply(document)
        self.assertEqual(self.fingerprint(), before)
        document = self.plan()
        self.config.write_text(self.config.read_text() + '\n# operator edit\n')
        before = self.fingerprint()
        with self.assertRaisesRegex(MemoryError, 'stale or altered'):
            self.apply(document)
        self.assertEqual(self.fingerprint(), before)

    def test_plan_tampering_hash_mismatch_and_expiry_are_refused(self):
        self.populate()
        document = self.plan()
        altered = copy.deepcopy(document)
        protected = next(row for row in altered['plan']['artifacts'] if row['action'] == 'keep')
        protected['action'] = 'delete'
        protected['protected_by'] = []
        with self.assertRaises(MemoryError):
            self.apply(altered)
        with self.assertRaises(MemoryError):
            apply_retention(document, '0' * 64)
        old = self.plan(as_of=datetime.now(timezone.utc) - timedelta(days=2))
        with self.assertRaisesRegex(MemoryError, '24 hours'):
            self.apply(old)

    def test_missing_referenced_evidence_refuses_all_deletion(self):
        self.populate()
        row = next(r for r in self.plan()['plan']['artifacts'] if 'note_or_ledger_reference' in r['protected_by'])
        (self.state / row['path']).unlink()
        before = self.fingerprint()
        with self.assertRaisesRegex(MemoryError, 'unresolved'):
            self.plan()
        self.assertEqual(self.fingerprint(), before)

    def test_tampered_raw_artifact_or_filename_refuses_all_deletion(self):
        self.populate()
        path = next((self.state / 'evidence').iterdir())
        path.write_text(path.read_text() + '\n')
        before = self.fingerprint()
        with self.assertRaisesRegex(MemoryError, 'content disagree'):
            self.plan()
        self.assertEqual(self.fingerprint(), before)

    def test_symlink_and_hardlink_artifacts_are_refused(self):
        self.populate()
        path = next((self.state / 'evidence').iterdir())
        outside = self.root / 'outside.json'
        path.rename(outside)
        for mode in ('symlink', 'hardlink'):
            with self.subTest(mode=mode):
                if mode == 'symlink': path.symlink_to(outside)
                else: os.link(outside, path)
                original = outside.read_bytes()
                with self.assertRaises(MemoryError):
                    self.plan()
                self.assertEqual(outside.read_bytes(), original)
                path.unlink()

    def test_symlink_directory_ancestor_and_overlapping_roots_are_refused(self):
        self.populate()
        link = self.root / 'state-link'
        link.symlink_to(self.state, target_is_directory=True)
        for state in (link, self.vault, self.vault / 'nested'):
            if state.name == 'nested': state.mkdir()
            with self.subTest(state=state.name), self.assertRaises(MemoryError):
                plan_retention(self.config, self.vault, state, keep_days=7)
        folder = self.state / 'evidence'
        moved = self.root / 'moved-evidence'
        folder.rename(moved)
        folder.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(MemoryError):
            self.plan()

    def test_missing_partial_or_foreign_control_files_refuse_pruning(self):
        self.populate()
        path = self.vault / GUARD
        original = path.read_bytes()
        for invalid in ('{}', '[]', '{"x": {}, "x": {}}'):
            path.write_text(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(MemoryError):
                self.plan()
        path.write_bytes(original)
        registry = self.vault / SELECTIONS
        data = json.loads(registry.read_text())
        data['fixture']['scope'] = 'other'
        registry.write_text(json.dumps(data))
        with self.assertRaisesRegex(MemoryError, 'registry'):
            self.plan()

    def test_unsafe_pin_and_missing_enumeration_reference_are_refused(self):
        self.populate()
        path = self.state / PINS
        for unsafe in ('../outside', '/absolute', 'evidence/../../outside'):
            path.write_text(json.dumps({'version': 1, 'files': {unsafe: '0' * 64}}))
            with self.subTest(path=unsafe), self.assertRaisesRegex(MemoryError, 'unsafe'):
                self.plan()
        path.unlink()
        checkpoint_path = self.state / 'checkpoint.json'
        checkpoint = json.loads(checkpoint_path.read_text())
        checkpoint['enumerations'] = {}
        checkpoint_path.write_text(json.dumps(checkpoint))
        with self.assertRaisesRegex(MemoryError, 'incomplete checkpoint'):
            self.plan()

    def test_unlink_failure_reports_partial_progress_and_allows_new_plan(self):
        self.populate()
        document = self.plan()
        real_unlink = os.unlink
        attempts = []

        def unlink(path, **kwargs):
            if 'dir_fd' in kwargs:
                attempts.append(path)
                if len(attempts) == 2:
                    raise OSError('synthetic unlink failure')
            return real_unlink(path, **kwargs)

        with patch('knowledge_memory.retention.os.unlink', unlink):
            result = self.apply(document)
        self.assertFalse(result['completed'])
        self.assertEqual(len(result['removed']), 1)
        self.assertFalse((self.vault / '.km-sync.lock').exists())
        self.assertTrue(self.apply(self.plan())['completed'])

    def test_unknown_files_and_invalid_policy_are_refused(self):
        self.populate()
        for keep_latest in (0, -1, True):
            with self.subTest(keep_latest=keep_latest), self.assertRaises(MemoryError):
                self.plan(keep_latest=keep_latest)
        (self.state / 'evidence' / 'operator-notes.txt').write_text('keep this')
        with self.assertRaisesRegex(MemoryError, 'unexpected'):
            self.plan()

    def test_unbound_sources_are_protected_without_claiming_they_are_current(self):
        self.populate()
        original = json.loads(next((self.state / 'evidence').iterdir()).read_text())
        foreign_id = 'f' * 32
        original['raw']['page']['id'] = foreign_id
        raw = encoded(original).encode()
        group = hashlib.sha256(('notion:' + foreign_id).encode()).hexdigest()
        name = group + '-' + hashlib.sha256(raw).hexdigest() + '.json'
        path = self.state / 'evidence' / name
        path.write_bytes(raw)
        os.utime(path, (self.old_time, self.old_time))
        document = self.plan()
        row = next(r for r in document['plan']['artifacts'] if r['path'] == 'evidence/' + name)
        self.assertIn('unknown_source', row['protected_by'])
        self.apply(document)
        self.assertEqual(path.read_bytes(), raw)

    def test_github_evidence_and_legacy_selection_use_the_same_retention_contract(self):
        self.config.write_text(yaml.safe_dump({'scope': 'demo', 'sources': [
            {'provider': 'github', 'repo': 'example/demo', 'path': 'guide.md', 'ref': 'main'}]}))
        for i in range(5):
            def fetch(*args, **kwargs):
                return {'type': 'file', 'encoding': 'base64', 'sha': str(i) * 40,
                        'content': base64.b64encode(f'evidence version {i}'.encode()).decode()}
            run(self.config, self.vault, self.db, self.state, fetch=fetch)
            for path in (self.state / 'evidence').iterdir():
                if path not in self.previous_files:
                    os.utime(path, (self.old_time + i, self.old_time + i))
                    self.previous_files.add(path)
        document = self.plan()
        self.assertEqual(document['plan']['summary']['evidence']['delete_files'], 3)
        self.assertTrue(self.apply(document)['completed'])
        self.assertTrue(search_notes(self.db, 'version 4', 'demo', include_candidates=True))

    def test_cli_preview_and_refusal_do_not_expose_raw_content(self):
        self.populate()
        output = io.StringIO()
        with patch('sys.stdout', output):
            code = main(['plan', '--config', str(self.config), '--vault', str(self.vault),
                         '--state', str(self.state), '--keep-days', '7'])
        self.assertEqual(code, 0)
        self.assertNotIn('synthetic-request-', output.getvalue())
        self.assertTrue(json.loads(output.getvalue())['dry_run'])
        path = self.root / 'saved-plan.json'
        path.write_text(output.getvalue())
        with patch('sys.stderr', io.StringIO()) as error:
            code = main(['apply', '--plan', str(path), '--expect-sha256', '0' * 64])
        self.assertEqual(code, 2)
        self.assertNotIn(str(self.root), error.getvalue())
        with patch('sys.stdout', io.StringIO()) as result:
            code = main(['apply', '--plan', str(path), '--expect-sha256', json.loads(output.getvalue())['plan']['plan_sha256']])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(result.getvalue())['completed'])

    def assert_malformed_raw_refused(self, malformed_raw):
        document = self.plan()
        candidate = next(r for r in document['plan']['artifacts'] if r['action'] == 'delete')
        path = self.state / candidate['path']
        original = path.read_bytes()
        value = json.loads(original)
        value['raw'] = malformed_raw
        raw = encoded(value).encode()
        replacement = path.with_name(candidate['group'] + '-' + hashlib.sha256(raw).hexdigest() + '.json')
        replacement.write_bytes(raw)
        path.unlink()
        saved = self.root / 'saved-plan.json'
        saved.write_text(json.dumps(document))
        before = self.fingerprint()
        try:
            for command in (
                ['plan', '--config', str(self.config), '--vault', str(self.vault),
                 '--state', str(self.state), '--keep-days', '7'],
                ['apply', '--plan', str(saved), '--expect-sha256', document['plan']['plan_sha256']],
            ):
                with self.subTest(command=command[0]), patch('sys.stderr', io.StringIO()) as error:
                    self.assertEqual(main(command), 2)
                    diagnostic = json.loads(error.getvalue())
                    self.assertEqual(diagnostic['error'], 'retention refused')
                    self.assertNotIn(str(self.root), error.getvalue())
                    self.assertNotIn('private-synthetic-marker', error.getvalue())
                    self.assertEqual(self.fingerprint(), before)
        finally:
            replacement.unlink()
            path.write_bytes(original)
            os.utime(path, (self.old_time, self.old_time))

    def test_cli_refuses_non_string_notion_identity_without_traceback_or_deletion(self):
        self.populate()
        for page_id in (None, 42, ['private-synthetic-marker']):
            with self.subTest(page_id=page_id):
                self.assert_malformed_raw_refused({'page': {'id': page_id}})

    def test_cli_refuses_non_string_github_content_without_traceback_or_deletion(self):
        self.populate()
        for content in (None, 42, {'private-synthetic-marker': 'keep private'}):
            with self.subTest(content=content):
                self.assert_malformed_raw_refused({'type': 'file', 'encoding': 'base64', 'content': content})

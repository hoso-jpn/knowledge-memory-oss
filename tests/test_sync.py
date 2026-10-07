import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml

from knowledge_memory.core import MemoryError, build_index, get_note, read_notes, search_notes
from knowledge_memory.sync import FetchError, GUARD, identifier, load_config, request, sync


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.config = self.root / 'allowlist.yaml'
        self.sources = [{'provider': 'github', 'repo': 'example/demo', 'path': 'guide.md', 'ref': 'main'}]
        self.save()
        self.calls = []

    def save(self):
        self.config.write_text(yaml.safe_dump({'scope': 'demo', 'sources': self.sources}))

    def fetch(self, provider, path):
        self.calls.append((provider, path))
        return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(b'original evidence').decode(), 'sha': 'a' * 40}

    def run_sync(self, **kwargs):
        return sync(self.config, self.vault, self.state, fetch=self.fetch, **kwargs)

    def test_dry_run_writes_nothing_and_fetches_only_allowlist(self):
        result = self.run_sync(dry_run=True)
        self.assertEqual(result['sources'][0]['status'], 'created')
        self.assertFalse(self.vault.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.calls, [('github', '/repos/example/demo/contents/guide.md?ref=main')])

    def test_repeated_source_does_not_duplicate_or_modify_note(self):
        self.run_sync()
        files = list(self.vault.rglob('*.md'))
        before = files[0].read_bytes()
        result = self.run_sync()
        self.assertEqual(result['sources'][0]['status'], 'unchanged')
        self.assertEqual(files[0].read_bytes(), before)
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 1)

    def test_inaccessible_source_blocks_search_get_context_inputs_without_deleting(self):
        self.run_sync()
        db = self.root / 'db.sqlite3'
        build_index(self.vault, db)
        note_id = identifier(self.sources[0])
        before = get_note(db, note_id, 'demo', True)['content']
        with patch('knowledge_memory.sync.fetch_note', side_effect=FetchError('missing_or_inaccessible')):
            self.run_sync()
        self.assertEqual(search_notes(db, 'evidence', 'demo', include_candidates=True), [])
        self.assertEqual(read_notes(db, 'demo', True, True), [])
        with self.assertRaises(MemoryError):
            get_note(db, note_id, 'demo', True, True)
        self.assertEqual(next(self.vault.rglob('*.md')).read_text(), before)

    def test_partial_failure_resume_retries_only_unfinished_sources(self):
        self.sources.append(dict(self.sources[0], path='other.md'))
        self.save()
        def fetch(provider, path):
            if 'other.md' in path:
                raise FetchError('fetch_failed')
            return self.fetch(provider, path)
        first = sync(self.config, self.vault, self.state, fetch=fetch)
        self.assertFalse(first['complete'])
        self.calls.clear()
        self.assertTrue(self.run_sync(resume=True)['complete'])
        self.assertEqual(len(self.calls), 1)
        self.assertIn('other.md', self.calls[0][1])

    def test_changed_allowlist_cannot_resume_old_run(self):
        with patch('knowledge_memory.sync.fetch_note', side_effect=FetchError('fetch_failed')):
            self.run_sync()
        self.sources[0]['ref'] = 'other'
        self.save()
        with self.assertRaises(MemoryError):
            self.run_sync(resume=True)

    def test_resume_rejects_another_vault_without_writing(self):
        self.sources.append(dict(self.sources[0], path='other.md'))
        self.save()

        def fetch(provider, path):
            if 'other.md' in path:
                raise FetchError('fetch_failed')
            return self.fetch(provider, path)

        self.assertFalse(sync(self.config, self.vault, self.state, fetch=fetch)['complete'])
        checkpoint = (self.state / 'checkpoint.json').read_bytes()
        other_vault = self.root / 'other-vault'
        self.calls.clear()
        with self.assertRaises(MemoryError):
            sync(self.config, other_vault, self.state, resume=True, fetch=self.fetch)
        self.assertFalse(other_vault.exists())
        self.assertEqual(self.calls, [])
        self.assertEqual((self.state / 'checkpoint.json').read_bytes(), checkpoint)

    def test_resume_rejects_checkpoint_without_vault_binding(self):
        with patch('knowledge_memory.sync.fetch_note', side_effect=FetchError('fetch_failed')):
            self.run_sync()
        path = self.state / 'checkpoint.json'
        journal = json.loads(path.read_text())
        journal.pop('vault')
        path.write_text(json.dumps(journal))
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.calls.clear()
        with self.assertRaisesRegex(MemoryError, 'matching interrupted run'):
            self.run_sync(resume=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertTrue(self.run_sync()['complete'])

    def test_local_edit_is_kept_and_requires_review(self):
        self.run_sync()
        note = next(self.vault.rglob('*.md'))
        note.write_text(note.read_text() + '\nlocal edit\n')
        before = note.read_bytes()
        result = self.run_sync()
        self.assertEqual(result['sources'][0]['status'], 'locally_modified')
        self.assertEqual(note.read_bytes(), before)
        ledger = json.loads((self.vault / GUARD).read_text())
        self.assertEqual(next(iter(ledger.values()))['status'], 'needs_review')

    def test_malformed_ledger_fails_closed(self):
        self.run_sync()
        db = self.root / 'db.sqlite3'
        build_index(self.vault, db)
        (self.vault / GUARD).write_text('[]')
        with self.assertRaises(MemoryError):
            search_notes(db, 'evidence', 'demo', include_candidates=True)

    def test_paths_and_duplicates_rejected(self):
        self.sources[0]['path'] = '../secret'
        self.save()
        with self.assertRaises(MemoryError):
            load_config(self.config)

    def test_transport_does_not_echo_token_or_response_body(self):
        import urllib.error
        import io
        error = urllib.error.HTTPError('https://api.github.com', 403, 'secret', {}, io.BytesIO(b'private token'))
        with patch('urllib.request.OpenerDirector.open', side_effect=error):
            with self.assertRaises(FetchError) as caught:
                request('github', '/repos/example/demo/contents/guide.md')
        self.assertEqual(str(caught.exception), 'inaccessible')

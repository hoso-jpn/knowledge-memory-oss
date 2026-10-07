import json
from pathlib import Path
import shutil
import tempfile
import unittest

from knowledge_memory.core import MemoryError, build_index, search_notes
from knowledge_memory.hybrid import CharacterEncoder, build_vectors, compare, hybrid_search

FIXTURE = Path(__file__).resolve().parents[1] / 'examples/context-vault'


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / 'vault'
        shutil.copytree(FIXTURE, self.vault)
        self.db, self.vectors = self.root / 'db.sqlite3', self.root / 'vectors.sqlite3'
        self.encoder = CharacterEncoder()
        build_index(self.vault, self.db)
        build_vectors(self.db, self.vectors, 'demo', self.encoder)

    def search(self, query, **kwargs):
        return hybrid_search(self.db, self.vectors, 'demo', query, self.encoder, **kwargs)

    def test_literal_order_and_short_terms_are_preserved(self):
        for query in ('線形', '条件', '架空', '線'):
            expected = [r['id'] for r in search_notes(self.db, query, 'demo')]
            actual = [r['id'] for r in self.search(query)]
            self.assertEqual(actual[:len(expected)], expected)

    def test_exact_id_is_retrievable(self):
        self.assertEqual(self.search('decision-model')[0]['id'], 'decision-model')

    def test_state_scope_and_provenance_survive_even_permissive_threshold(self):
        hits = self.search('モデル', threshold=-1, limit=50)
        ids = {r['id'] for r in hits}
        self.assertFalse(ids & {'note-candidate', 'note-withdrawn', 'note-other'})
        for hit in hits:
            self.assertIn('sources', hit['metadata'])
            self.assertEqual(len(hit['sha256']), 64)

    def test_source_update_requires_vector_rebuild(self):
        path = self.vault / '10-notes/state.md'
        path.write_text(path.read_text() + '\nchanged\n')
        build_index(self.vault, self.db)
        with self.assertRaisesRegex(MemoryError, 'stale'):
            self.search('モデル')
        build_vectors(self.db, self.vectors, 'demo', self.encoder)
        self.assertTrue(self.search('モデル'))

    def test_model_change_rejected(self):
        self.encoder.identity = dict(self.encoder.identity, changed=True)
        with self.assertRaises(MemoryError):
            self.search('モデル')

    def test_cannot_overwrite_source_index(self):
        before = self.db.read_bytes()
        link = self.root / 'hard.sqlite3'
        link.hardlink_to(self.db)
        for target in (self.db, link):
            with self.assertRaises(MemoryError):
                build_vectors(self.db, target, 'demo', self.encoder)
        self.assertEqual(before, self.db.read_bytes())

    def test_compare_records_improvements_and_regressions(self):
        result = compare(self.db, self.vectors, 'demo', [
            {'id': 'id', 'query': 'decision-model', 'expect_ids': ['decision-model']}], self.encoder)
        self.assertEqual(result['improvements'], ['id'])
        self.assertFalse(result['encoder']['semantic'])
        self.assertFalse(result['gpu_required'])

    def test_deleted_record_is_removed_from_rebuilt_vectors(self):
        from knowledge_memory.staging import note_text
        import sqlite3
        extra = self.vault / 'extra.md'
        extra.write_text(note_text(dict(id='extra', title='extra', kind='note', status='active',
            scope='demo', claim_kind='source_report', updated='2026-09-26',
            sources=[{'ref':'fixture:extra','locator':'all'}]), 'unique-extra-content'))
        build_index(self.vault, self.db)
        build_vectors(self.db, self.vectors, 'demo', self.encoder)
        extra.unlink()
        build_index(self.vault, self.db)
        with self.assertRaises(MemoryError):
            self.search('extra')
        build_vectors(self.db, self.vectors, 'demo', self.encoder)
        with sqlite3.connect(self.vectors) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM vectors WHERE id='extra'").fetchone()[0], 0)

    def test_comparison_does_not_pass_partial_multi_record_evidence(self):
        from unittest.mock import patch
        questions = [{'id': 'cross-note', 'query': 'model',
                      'expect_ids': ['decision-model', 'note-conflict'], 'min_hits': 2}]
        with patch('knowledge_memory.hybrid.search_notes', return_value=[{'id': 'decision-model'}]), \
                patch('knowledge_memory.hybrid.hybrid_search', return_value=[
                    {'id': 'decision-model'}, {'id': 'note-conflict'}]):
            report = compare(self.db, self.vectors, 'demo', questions, self.encoder)
        self.assertEqual(report['success'], {'literal': 0, 'hybrid': 1})
        self.assertEqual(report['improvements'], ['cross-note'])

    def test_compare_cli_accepts_the_existing_yaml_question_set(self):
        import subprocess
        import sys
        path = self.root / 'questions.yaml'
        path.write_text('questions:\n  - id: unknown\n    query: missing\n    expect: unknown\n')
        result = subprocess.run([sys.executable, '-m', 'knowledge_memory.hybrid', 'compare',
            '--db', str(self.db), '--vectors', str(self.vectors), '--scope', 'demo',
            '--questions', str(path)], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['questions'], 1)

    def test_legacy_json_and_invalid_multi_record_threshold(self):
        from knowledge_memory.evaluation import load_questions
        path = Path(__file__).parents[1] / 'evaluation/hybrid-synthetic.json'
        questions = load_questions(path, allow_legacy_list=True)
        self.assertEqual(questions[-1]['expect'], 'unknown')
        self.assertEqual(compare(self.db, self.vectors, 'demo', questions, self.encoder)['questions'], 6)
        for value in (True, 0, 3):
            questions = [{'id': 'bad', 'query': 'model', 'expect_ids': ['decision-model'], 'min_hits': value}]
            with self.assertRaises(MemoryError):
                compare(self.db, self.vectors, 'demo', questions, self.encoder)

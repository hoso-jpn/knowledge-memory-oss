import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from knowledge_memory.core import MemoryError
from knowledge_memory.live_acceptance import run
from knowledge_memory.sync import FetchError
from test_data_source_sync import FakeAPI
from test_knowledge_import import CONFIG, DS_ID


class LiveAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'run'
        self.config, self.questions = self.root / 'sync.yaml', self.root / 'questions.yaml'
        (self.root / 'import.yaml').write_text(yaml.safe_dump(CONFIG, allow_unicode=True))
        self.config.write_text(yaml.safe_dump({'selection_id': 'knowledge', 'scope': 'demo', 'sources': [
            {'provider': 'notion', 'data_source_id': DS_ID, 'import_config': 'import.yaml'}]}))
        self.questions.write_text(yaml.safe_dump({'questions': [
            {'id': 'cross', 'query': '本文', 'expect_ids': ['src-' + f'{i:032x}' for i in (1, 2)], 'min_hits': 2},
            {'id': 'unknown', 'query': '存在しない量子宇宙船', 'expect': 'unknown'}]}))
        self.api = FakeAPI()

    def accept(self, **kwargs):
        return run(self.config, self.questions, self.workspace, include_candidates=True, fetch=self.api, **kwargs)

    def test_complete_pipeline_keeps_manual_and_llm_checks_unverified(self):
        config_before, questions_before = self.config.read_bytes(), self.questions.read_bytes()
        result = self.accept(with_mcp=True)
        self.assertEqual(result['automated_status'], 'passed', result)
        self.assertEqual(result['records'], 2)
        self.assertEqual(result['evaluation']['hit'], 1)
        self.assertEqual(result['evaluation']['false_hit'], 0)
        self.assertTrue(result['mcp']['retrieval_verified'])
        self.assertFalse(result['mcp']['llm_initiated'])
        self.assertTrue(all(v == 'not_run' for v in result['manual_checks'].values()))
        self.assertEqual(json.loads((self.workspace / 'report.json').read_text()), result)
        self.assertTrue(all(p['status'] == 'candidate' for p in result['provenance']))
        self.assertTrue(all(p['origin']['data_source_id'] == DS_ID for p in result['provenance']))
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(self.questions.read_bytes(), questions_before)

    def test_partial_dry_run_cannot_proceed_or_report_success(self):
        self.api.fail['/pages/' + f'{2:032x}'] = FetchError('retry_later')
        result = self.accept()
        self.assertEqual(result['automated_status'], 'failed')
        self.assertEqual(result['stage'], 'dry_run')
        self.assertFalse((self.workspace / 'vault').exists())
        self.assertFalse((self.workspace / 'index.sqlite3').exists())
        self.assertEqual(list(result['sync_runs']), ['dry_run'])

    def test_empty_data_source_cannot_masquerade_as_success(self):
        self.api.members[DS_ID] = []
        self.assertEqual(self.accept()['automated_status'], 'failed')
        self.assertFalse((self.workspace / 'index.sqlite3').exists())

    def test_initial_sync_failure_preserves_partial_evidence_without_indexing(self):
        underlying = self.api
        queries = 0
        def failing(provider, path, method='GET', body=None):
            nonlocal queries
            if method == 'POST':
                queries += 1
                if queries == 2:
                    underlying.fail['/pages/' + f'{2:032x}'] = FetchError('inaccessible')
            return underlying(provider, path, method, body)
        self.api = failing
        result = self.accept()
        self.assertEqual(result['automated_status'], 'failed')
        self.assertEqual(result['stage'], 'initial_sync')
        self.assertEqual(len(list((self.workspace / 'vault').rglob('*.md'))), 1)
        self.assertTrue((self.workspace / 'sync-state/checkpoint.json').exists())
        self.assertFalse((self.workspace / 'index.sqlite3').exists())

    def test_retrieval_miss_is_not_a_successful_acceptance_run(self):
        questions = yaml.safe_load(self.questions.read_text())
        questions['questions'][0]['query'] = '未収録の検索語'
        self.questions.write_text(yaml.safe_dump(questions))
        result = self.accept()
        self.assertEqual(result['automated_status'], 'failed')
        self.assertEqual(result['evaluation']['miss'], 1)
        self.assertFalse(result['checks']['fixed_questions_passed'])

    def test_provenance_preserves_distinct_source_dates(self):
        result = self.accept()
        self.assertEqual(result['automated_status'], 'passed')
        for row in result['provenance']:
            self.assertEqual(row['source_edited_at'], '2026-09-01T10:00:00Z')
            self.assertEqual(row['document_date'],
                             {'start': '2025-02-03', 'end': None, 'time_zone': None})
            self.assertNotEqual(row['source_edited_at'], row['origin']['retrieved_at'])

    def test_failed_retrieval_stops_before_the_optional_sdk_probe(self):
        questions = yaml.safe_load(self.questions.read_text())
        questions['questions'][0]['query'] = '未収録の検索語'
        self.questions.write_text(yaml.safe_dump(questions))
        with patch('knowledge_memory.live_acceptance.probe_mcp',
                   return_value={'retrieval_verified': True}) as probe:
            result = self.accept(with_mcp=True)
        probe.assert_not_called()
        self.assertEqual(result['automated_status'], 'failed')
        self.assertEqual(result['stage'], 'index_and_retrieval')
        self.assertEqual(result['evaluation']['miss'], 1)
        self.assertEqual(result['mcp']['status'], 'not_run')

    def test_live_change_during_repeat_retains_evidence_and_fails(self):
        underlying = self.api
        queries = 0
        def changing(provider, path, method='GET', body=None):
            nonlocal queries
            if method == 'POST':
                queries += 1
                if queries == 3:
                    underlying.records[f'{1:032x}']['page']['properties']['整理状態']['select']['name'] = '変更'
            return underlying(provider, path, method, body)
        self.api = changing
        result = self.accept()
        self.assertEqual(result['automated_status'], 'failed')
        self.assertEqual(result['stage'], 'repeat_sync')
        self.assertFalse(result['checks']['repeat_unchanged'])
        self.assertTrue((self.workspace / 'sync-state/evidence').is_dir())
        self.assertFalse((self.workspace / 'index.sqlite3').exists())

    def test_input_edits_cannot_widen_selection_between_stages(self):
        underlying = self.api
        def changing(provider, path, method='GET', body=None):
            self.config.write_text('broken: changed by another editor')
            return underlying(provider, path, method, body)
        self.api = changing
        self.assertEqual(self.accept()['automated_status'], 'passed')
        self.assertIn('sources:', (self.workspace / 'inputs/sync.yaml').read_text())

    def test_existing_or_repository_workspace_is_rejected_before_fetch(self):
        self.workspace.mkdir()
        with self.assertRaises(MemoryError):
            self.accept()
        self.workspace = self.root / 'repo' / 'run'
        (self.root / 'repo' / '.git').mkdir(parents=True)
        with self.assertRaises(MemoryError):
            self.accept()
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.api.calls, [])

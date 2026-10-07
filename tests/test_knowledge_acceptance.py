"""CLI subprocesses and official SDK against synthetic learning notes, never real user data."""
import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from knowledge_memory.core import build_index, get_note
from knowledge_memory.operations import backup, restore, recovery_check
from knowledge_memory.sync import GUARD, SELECTIONS, FetchError, sync
from test_data_source_sync import FakeAPI, DS_ID
import yaml

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / 'examples/knowledge-notion'
IMPORT_CONFIG = ROOT / 'examples/config/knowledge-import.yaml'
NOTE_ID = 'src-' + f'{1:032x}'


class KnowledgeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.vault = self.root / 'vault'; self.db = self.root / 'index.sqlite3'
        self.cli('import-notion', '--snapshot', FIXTURE, '--vault', self.vault,
                 '--scope', 'demo', '--import-config', IMPORT_CONFIG)
        self.cli('index', '--vault', self.vault, '--db', self.db)

    def cli(self, *args, success=True):
        result = subprocess.run([sys.executable, '-m', 'knowledge_memory', *map(str, args)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0 if success else 2, result.stderr)
        return json.loads(result.stdout) if success else result.stderr

    def test_four_cli_records_provenance_and_idempotence(self):
        first = {p.name: p.read_bytes() for p in self.vault.rglob('*.md')}
        result = self.cli('import-notion', '--snapshot', FIXTURE, '--vault', self.vault,
                          '--scope', 'demo', '--import-config', IMPORT_CONFIG)
        self.assertEqual(len(result['unchanged']), 4)
        self.assertEqual(first, {p.name: p.read_bytes() for p in self.vault.rglob('*.md')})
        self.assertEqual(self.cli('search', '--db', self.db, '--scope', 'demo', '工房の三層'), [])
        hits = self.cli('search', '--db', self.db, '--scope', 'demo', '--include-candidates', '工房の三層')
        self.assertEqual(hits[0]['id'], NOTE_ID)
        for i in range(1, 5):
            got = self.cli('get', '--db', self.db, '--scope', 'demo', '--include-candidates', 'src-' + f'{i:032x}')
            self.assertEqual((got['metadata']['status'], got['metadata']['claim_kind']), ('candidate', 'source_report'))
            origin = got['metadata']['origin']
            self.assertIn('source_edited_at', origin); self.assertIn('retrieved_at', origin)
            self.assertIn('document_date', origin['source_metadata'])
            for heading in ('研修要約', '公式補足', '適用案', '末尾の比較表'):
                self.assertIn('## ' + heading, got['content'])
            self.assertIn('末尾証拠' + str(i), got['content'])
            self.assertIn(f'https://example.org/synthetic/{i}', got['content'])
            self.assertNotIn('X-Amz', got['content'])
        self.cli('get', '--db', self.db, '--scope', 'other', '--include-candidates', NOTE_ID, success=False)

    def test_fixed_retrieval_questions_include_cross_note_and_unknown(self):
        args = ['evaluate', '--db', self.db, '--scope', 'demo', '--questions', ROOT / 'evaluation/knowledge-synthetic-questions.yaml']
        candidates = self.cli(*args, '--include-candidates')
        self.assertEqual((candidates['hit'], candidates['miss'], candidates['false_hit']), (6, 0, 0))
        ordinary = self.cli(*args)
        self.assertEqual((ordinary['hit'], ordinary['miss'], ordinary['false_hit']), (0, 6, 0))

    def test_long_context_marks_omission_and_get_recovers_tail_with_dates(self):
        out = self.root / 'context.md'
        result = self.cli('context', '--db', self.db, '--scope', 'demo', '--include-candidates',
                          '--goal', '工房の三層', '--budget', '2600', '--out', out)
        self.assertIn(NOTE_ID, result['truncated'])
        text = out.read_text()
        self.assertLessEqual(len(text), 2600)
        self.assertIn('引用はここで切れています', text)
        self.assertNotIn('末尾証拠1', text)
        for field in ('取得時点:', '原本編集時点:', '資料日: 2025-02-03', 'source_report', 'candidate'):
            self.assertIn(field, text)
        got = self.cli('get', '--db', self.db, '--scope', 'demo', '--include-candidates', NOTE_ID)
        self.assertIn('末尾証拠1', got['content'])
        self.assertEqual(got['content'].count('根拠として保持する本文。'), 0)
        old = self.cli('get', '--db', self.db, '--scope', 'demo', '--include-candidates', 'src-' + f'{2:032x}')
        self.assertEqual(old['metadata']['origin']['source_metadata']['document_date']['start'], '2020-01-01')
        self.assertNotIn('verified_at', old['metadata'])

    def test_changed_input_requires_reindex_and_does_not_auto_promote(self):
        snapshot = self.root / 'snapshot'; shutil.copytree(FIXTURE, snapshot)
        record_path = snapshot / f'{1:032x}.json'; record = json.loads(record_path.read_text())
        record['blocks'][1]['paragraph']['rich_text'][0]['plain_text'] = '明示更新を再参照する人工文'
        record_path.write_text(json.dumps(record))
        self.cli('import-notion', '--snapshot', snapshot, '--vault', self.vault,
                 '--scope', 'demo', '--import-config', IMPORT_CONFIG)
        self.cli('search', '--db', self.db, '--scope', 'demo', '--include-candidates', '明示更新', success=False)
        self.cli('index', '--vault', self.vault, '--db', self.db)
        hit = self.cli('search', '--db', self.db, '--scope', 'demo', '--include-candidates', '明示更新')[0]
        self.assertEqual(hit['id'], NOTE_ID)
        self.assertEqual(hit['metadata']['status'], 'candidate')

    def test_recovery_keeps_candidates_and_selection_ownership(self):
        registry = {'demo-profile': {'scope': 'demo', 'containers': ['notion-data-source:' + DS_ID]}}
        (self.vault / SELECTIONS).write_text(json.dumps(registry))
        guard = {'notion:' + f'{2:032x}': {'note_id': 'src-' + f'{2:032x}', 'scope': 'demo', 'status': 'inaccessible',
                                          'selection_id': 'demo-profile', 'data_source_id': DS_ID}}
        (self.vault / GUARD).write_text(json.dumps(guard))
        result = recovery_check(self.vault, 'demo', ['工房の三層'], include_candidates=True)
        self.assertTrue(result['same_bytes']); self.assertTrue(result['queries'][0]['same_results'])
        self.assertEqual(result['queries'][0]['result_ids'], [NOTE_ID])
        archive = self.root / 'backup.zip'; target = self.root / 'recovered'
        backup(self.vault, archive); restore(archive, target)
        self.assertEqual((target / SELECTIONS).read_bytes(), (self.vault / SELECTIONS).read_bytes())
        self.assertEqual((target / GUARD).read_bytes(), (self.vault / GUARD).read_bytes())

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'install the mcp extra')
    def test_live_sdk_candidates_and_sync_hold_cannot_be_bypassed(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        api = FakeAPI()
        api.records = {p.stem: json.loads(p.read_text()) for p in FIXTURE.glob('*.json') if p.name != 'manifest.json'}
        for record in api.records.values(): record['blocks'] = []  # failures below never read bodies
        api.members[DS_ID] = list(api.records)
        config = self.root / 'sync.yaml'
        config.write_text(yaml.safe_dump({'selection_id': 'demo-learning', 'scope': 'demo', 'sources': [
            {'provider': 'notion', 'data_source_id': DS_ID, 'import_config': str(IMPORT_CONFIG)}]}))
        async def run():
            parameters = StdioServerParameters(command=sys.executable, args=[
                '-m', 'knowledge_memory.mcp_server', '--vault', str(self.vault), '--db', str(self.db),
                '--scope', 'demo', '--include-candidates'])
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    hits = await session.call_tool('search_memory', {'query': '工房の三層'})
                    self.assertFalse(hits.isError)
                    self.assertEqual(json.loads(hits.content[0].text)[0]['id'], NOTE_ID)
                    note = await session.call_tool('get_memory', {'identifier': NOTE_ID})
                    self.assertIn('末尾証拠1', json.loads(note.content[0].text)['content'])
                    api.fail['/data_sources/' + DS_ID] = FetchError('inaccessible')
                    sync(config, self.vault, self.root / 'sync-state', fetch=api)
                    held = await session.call_tool('search_memory', {'query': '工房の三層'})
                    self.assertEqual(json.loads(held.content[0].text), [])
                    denied = await session.call_tool('get_memory', {'identifier': NOTE_ID})
                    self.assertTrue(denied.isError)
                    context = await session.call_tool('build_context', {'goal': '工房の三層'})
                    self.assertEqual(json.loads(context.content[0].text)['records'], [])
                    widened = await session.call_tool('get_memory', {'identifier': NOTE_ID, 'include_history': True})
                    self.assertTrue(widened.isError)
        asyncio.run(asyncio.wait_for(run(), 30))

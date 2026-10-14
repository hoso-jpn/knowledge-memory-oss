import asyncio
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from knowledge_memory.core import MemoryError, build_index, get_note, search_notes
from knowledge_memory.operations import backup, benchmark, peak_rss, probe_mcp, recovery_check, restore, time_comparison

FIXTURE = Path(__file__).resolve().parents[1] / 'examples/context-vault'


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_snapshot_rebuild_preserves_bytes_and_retrieval(self):
        result = recovery_check(FIXTURE, 'demo', ['線形', '条件', '存在しない量子宇宙船'])
        self.assertTrue(result['same_bytes'])
        self.assertTrue(all(q['same_results'] for q in result['queries']))

    def test_restore_never_overwrites_existing_vault(self):
        archive = self.root / 'backup.zip'
        backup(FIXTURE, archive)
        with self.assertRaises(MemoryError):
            restore(archive, FIXTURE)
        with self.assertRaises(MemoryError):
            backup(FIXTURE, archive)

    def test_manifest_tampering_and_traversal_refused(self):
        for name in ('../../escape.md', '/absolute.md', 'safe.md'):
            path = self.root / 'bad.zip'
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('manifest.json', json.dumps({'version':1,'files':{name:{'sha256':'0'*64,'bytes':1}}}))
                z.writestr('vault/' + name, 'x')
            target = self.root / 'recovered'
            with self.assertRaises(MemoryError):
                restore(path, target)
            self.assertFalse(target.exists())

    def test_states_and_unknown_queries_after_rebuild(self):
        db = self.root / 'index.sqlite3'
        build_index(FIXTURE, db)
        self.assertEqual(search_notes(db, '量子宇宙船', 'demo'), [])
        for identifier in ('note-candidate', 'note-withdrawn', 'note-other'):
            with self.assertRaises(MemoryError):
                get_note(db, identifier, 'demo')
        self.assertIn('claim_kind', get_note(db, 'idea-threshold', 'demo')['metadata'])

    def test_benchmark_reports_hash_cost_and_environment(self):
        result = benchmark((3, 8), repeats=2)
        self.assertEqual([r['records'] for r in result['rows']], [3, 8])
        self.assertEqual(len(result['rows'][0]['hash_seconds']), 2)
        self.assertFalse(result['environment']['cloud_auto_connected'])
        self.assertIsNone(result['environment']['electricity_cost'])


    def test_peak_rss_is_optional_without_resource_module(self):
        self.assertIsNone(peak_rss(None))

    def test_real_stdio_probe_distinguishes_cloud_and_model(self):
        db = self.root / 'index.sqlite3'
        build_index(FIXTURE, db)
        result = asyncio.run(asyncio.wait_for(probe_mcp(FIXTURE, db, 'demo', '線形'), 30))
        self.assertTrue(result['mcp_connected'])
        self.assertTrue(result['source_ids'])
        self.assertFalse(result['cloud_auto_connected'])
        self.assertFalse(result['llm_initiated'])

    def test_time_comparison_preserves_negative_savings(self):
        path = self.root / 'timings.json'
        path.write_text(json.dumps([{'reexplain_seconds':10,'organize_seconds':20,'retrieve_seconds':5}]))
        result = time_comparison(path)
        self.assertEqual(result['net_seconds_saved'], -15)

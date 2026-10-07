import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from knowledge_memory.core import MemoryError, build_index, read_notes, search_notes
from knowledge_memory.operations import measure

FIXTURE = Path(__file__).resolve().parents[1] / 'examples/context-vault'


class ExistingVaultMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.db = self.root / 'vault', self.root / 'index.sqlite3'
        shutil.copytree(FIXTURE, self.vault)
        build_index(self.vault, self.db)

    def test_cli_measures_existing_bytes_without_changing_files_or_exposing_text(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        run = subprocess.run([sys.executable, '-m', 'knowledge_memory.operations', 'measure',
                              '--vault', str(self.vault), '--db', str(self.db), '--scope', 'demo',
                              '--query', '線形', '--query', '量子宇宙船', '--repeats', '2'],
                             text=True, capture_output=True, check=True)
        result = json.loads(run.stdout)
        self.assertEqual(result['eligible_records'], len(read_notes(self.db, 'demo')))
        self.assertEqual(result['queries'][1]['hit_counts'], [0, 0])
        self.assertEqual(len(result['hash_seconds']), 2)
        self.assertFalse(result['index_rebuilt'])
        self.assertNotIn('線形', run.stdout)
        self.assertNotIn(str(self.vault), run.stdout)
        after = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_wrong_vault_and_stale_index_are_refused(self):
        with self.assertRaisesRegex(MemoryError, 'another vault'):
            measure(self.root / 'different', self.db, 'demo', ['線形'])
        note = next(self.vault.rglob('*.md'))
        note.write_bytes(note.read_bytes() + b'\nchanged')
        with self.assertRaisesRegex(MemoryError, 'rebuild'):
            measure(self.vault, self.db, 'demo', ['線形'])

    def test_candidate_selection_and_invalid_inputs(self):
        default = measure(self.vault, self.db, 'demo', ['線形'], repeats=1)
        candidates = measure(self.vault, self.db, 'demo', ['線形'], repeats=1, include_candidates=True)
        self.assertGreater(candidates['eligible_records'], default['eligible_records'])
        for repeats, queries in [(True, ['x']), (0, ['x']), (21, ['x']), (1, []), (1, [' '])]:
            with self.subTest(repeats=repeats, queries=queries), self.assertRaises(MemoryError):
                measure(self.vault, self.db, 'demo', queries, repeats)

    def test_control_change_during_measurement_is_refused(self):
        def changed(*args, **kwargs):
            result = search_notes(*args, **kwargs)
            (self.vault / '.km-sync-selections.json').write_text('{"changed":{}}')
            return result
        with patch('knowledge_memory.operations.search_notes', side_effect=changed):
            with self.assertRaisesRegex(MemoryError, 'changed during measurement'):
                measure(self.vault, self.db, 'demo', ['線形'], repeats=1)

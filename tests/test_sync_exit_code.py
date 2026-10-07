"""km-sync exit codes.

A dry run writes nothing, so it has no 'complete' flag, but it still learns whether every
selected source could be read. Reporting success regardless would let an unattended run
treat an empty result as a finished one, which is what these tests pin down.

Everything here is synthetic: a fixed body, a fake transport and a fake enumerator. No
network access and no real vault.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from knowledge_memory import sync as sync_module
from knowledge_memory.sync import FetchError, fetch_note, main

BODY = b'# Synthetic fixture\n\nA body invented for this test, not imported from anywhere.\n'
DS_ID = 'a' * 32
IMPORT_CONFIG = Path(__file__).resolve().parents[1] / 'examples' / 'config' / 'knowledge-import.yaml'


def transport(provider, path):
    """Serve the one GitHub file the allowlist names."""
    return {'type': 'file', 'encoding': 'base64',
            'content': base64.b64encode(BODY).decode(), 'sha': 'b' * 40}


class ExitCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.config = self.root / 'allowlist.yaml'
        self.write_config([{'provider': 'github', 'repo': 'example/demo',
                            'path': 'guide.md', 'ref': 'main'}])

    def write_config(self, sources, **extra):
        self.config.write_text(yaml.safe_dump({'scope': 'demo', 'sources': sources, **extra}))

    def invoke(self, *extra):
        """Run the CLI entry point and return (exit code, parsed report)."""
        argv = ['km-sync', '--config', str(self.config), '--vault', str(self.vault),
                '--state', str(self.state), *extra]
        out = io.StringIO()
        with patch.object(sys, 'argv', argv), patch.object(sys, 'stdout', out):
            code = main()
        printed = out.getvalue().strip()
        return code, json.loads(printed) if printed else None

    @staticmethod
    def reachable(source, scope, stamp, fetch=None):
        return fetch_note(source, scope, stamp, transport)

    def unreachable(self, status):
        return patch.object(sync_module, 'fetch_note', side_effect=FetchError(status))

    # --- dry runs ---------------------------------------------------------------

    def test_clean_dry_run_succeeds(self):
        with patch.object(sync_module, 'fetch_note', self.reachable):
            code, report = self.invoke('--dry-run')
        self.assertEqual(code, 0)
        self.assertEqual([r['status'] for r in report['sources']], ['created'])
        self.assertFalse(self.vault.exists())
        self.assertFalse(self.state.exists())

    def test_dry_run_fails_when_a_source_cannot_be_read(self):
        with self.unreachable('missing_or_inaccessible'):
            code, report = self.invoke('--dry-run')
        self.assertEqual(code, 2)
        self.assertEqual([r['status'] for r in report['sources']], ['missing_or_inaccessible'])
        self.assertFalse(self.vault.exists())

    def test_dry_run_fails_on_a_retryable_fetch_failure(self):
        with self.unreachable('retry_later'):
            code, _ = self.invoke('--dry-run')
        self.assertEqual(code, 2)

    def test_dry_run_fails_when_one_of_several_sources_cannot_be_read(self):
        self.write_config([{'provider': 'github', 'repo': 'example/demo',
                            'path': name, 'ref': 'main'} for name in ('a.md', 'b.md')])
        calls = []

        def partly(source, scope, stamp, fetch=None):
            calls.append(source['path'])
            if source['path'] == 'b.md':
                raise FetchError('missing_or_inaccessible')
            return fetch_note(source, scope, stamp, transport)

        with patch.object(sync_module, 'fetch_note', partly):
            code, report = self.invoke('--dry-run')
        self.assertEqual(code, 2)
        self.assertEqual(sorted(calls), ['a.md', 'b.md'])
        self.assertEqual(sorted(r['status'] for r in report['sources']),
                         ['created', 'missing_or_inaccessible'])

    def test_dry_run_fails_when_an_enumeration_fails(self):
        self.write_config([{'provider': 'notion', 'data_source_id': DS_ID,
                            'import_config': 'import.yaml'}], selection_id='knowledge')
        (self.root / 'import.yaml').write_text(IMPORT_CONFIG.read_text(encoding='utf-8'),
                                               encoding='utf-8')
        with patch.object(sync_module, '_enumerate_source', side_effect=FetchError('retry_later')):
            code, report = self.invoke('--dry-run')
        self.assertEqual(code, 2)
        self.assertEqual([e['status'] for e in report['enumerations']], ['retry_later'])

    def test_dry_run_succeeds_when_it_declines_to_overwrite_a_hand_edited_note(self):
        """locally_modified is a protective decision, not a failure to read the source."""
        with patch.object(sync_module, 'fetch_note', self.reachable):
            self.assertEqual(self.invoke()[0], 0)
            note = next(iter(self.vault.rglob('*.md')))
            note.write_text(note.read_text(encoding='utf-8') + '\nA later hand edit.\n',
                            encoding='utf-8')
            code, report = self.invoke('--dry-run')
        self.assertEqual([r['status'] for r in report['sources']], ['locally_modified'])
        self.assertEqual(code, 0)

    # --- normal runs ------------------------------------------------------------

    def test_normal_run_succeeds_when_complete(self):
        with patch.object(sync_module, 'fetch_note', self.reachable):
            code, report = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(report['complete'])

    def test_normal_run_fails_when_a_source_cannot_be_read(self):
        with self.unreachable('missing_or_inaccessible'):
            code, report = self.invoke()
        self.assertEqual(code, 2)
        self.assertFalse(report['complete'])


if __name__ == '__main__':
    unittest.main()

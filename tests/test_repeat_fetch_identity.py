"""Reading an unchanged Notion page twice must be recognised as unchanged.

Notion stamps each response envelope with a fresh request_id and hands out a fresh signed
URL for a hosted attachment. Neither belongs to the page, so neither may change the record
identity the vault compares against; otherwise every sync rewrites every note and a repeat
sync can never report unchanged.

Everything here is synthetic: an invented page, an invented attachment and a fake transport.
"""

from __future__ import annotations

import copy
import itertools
from pathlib import Path
import re
import urllib.parse
import shutil
from tempfile import TemporaryDirectory
import unittest

import yaml

from knowledge_memory.core import parse_note
from knowledge_memory.notion_semantics import stable_record
from knowledge_memory.staging import render_digest
from knowledge_memory.sync import identifier, sync
from test_knowledge_import import CONFIG, PAGE_ID, knowledge_record, render, rt

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / 'examples/config/knowledge-import.yaml'
SOURCE = {'provider': 'notion', 'page_id': PAGE_ID, 'import_config': 'import.yaml'}


def attachment(edition):
    """A hosted file block: the signed URL and its expiry change on every read."""
    return {'id': '12345678-abcd-abcd-abcd-123456789abc', 'type': 'file',
            'file': {'type': 'file', 'name': '控え.md', 'caption': [rt('保存時点の控え')],
                     'file': {'url': 'https://files.example.org/object-1/notes.md'
                                     f'?X-Amz-Signature=sig{edition}&X-Amz-Date=day{edition}',
                              'expiry_time': f'2026-0{edition}-01T00:00:00Z'}}}


class Transport:
    """Serves one synthetic page, stamping a new request_id on every response."""

    def __init__(self, record):
        self.record = record
        for number, value in enumerate(record['page']['properties'].values()):
            value.setdefault('id', f'p{number}')   # a real page property carries an id
        self.editions = itertools.count(1)
        self.calls = []

    def envelope(self, payload):
        return {**payload, 'request_id': f'req-{next(self.editions):08d}'}

    def __call__(self, provider, path, method='GET', body=None):
        assert provider == 'notion' and method == 'GET', (provider, method)
        self.calls.append(path)
        page = copy.deepcopy(self.record['page'])
        if re.fullmatch(rf'/pages/{PAGE_ID}', path):
            return self.envelope({'object': 'page', **page})
        found = re.fullmatch(rf'/pages/{PAGE_ID}/properties/([^?]+)', path.split('?')[0])
        if found:
            wanted = urllib.parse.unquote(found.group(1))
            name = next(n for n, v in page['properties'].items() if v['id'] == wanted)
            return copy.deepcopy(page['properties'][name])
        if re.fullmatch(rf'/blocks/{PAGE_ID}/children', path.split('?')[0]):
            blocks = copy.deepcopy(self.record['blocks'])
            blocks.append(attachment(next(self.editions)))
            return self.envelope({'object': 'list', 'results': blocks,
                                  'has_more': False, 'next_cursor': None})
        if re.fullmatch(r'/blocks/[0-9a-f-]+/children', path.split('?')[0]):
            return self.envelope({'object': 'list', 'results': [],
                                  'has_more': False, 'next_cursor': None})
        raise AssertionError(path)


class EnvelopeNormalisationTests(unittest.TestCase):
    def test_an_envelope_request_id_is_not_part_of_the_record(self):
        page = {'object': 'page', 'id': PAGE_ID, 'request_id': 'req-1'}
        self.assertEqual(stable_record(page), {'object': 'page', 'id': PAGE_ID})
        self.assertIn('request_id', page)              # the input is not mutated

    def test_a_property_a_person_named_request_id_is_kept(self):
        record = {'object': 'page', 'request_id': 'req-1', 'properties': {
            'request_id': {'type': 'rich_text', 'rich_text': [rt('社内票番号')]}}}
        normalized = stable_record(record)
        self.assertNotIn('request_id', normalized)                       # the envelope's
        self.assertIn('request_id', normalized['properties'])            # the person's
        self.assertEqual(normalized['properties'], record['properties'])

    def test_an_object_without_an_envelope_key_is_untouched(self):
        plain = {'type': 'rich_text', 'request_id': 'a value that is content'}
        self.assertEqual(stable_record(plain), plain)


class RepeatReadTests(unittest.TestCase):
    def origin(self, record):
        return parse_note('note', render(record).encode()).metadata['origin']

    def test_a_rotating_request_id_leaves_the_rendered_note_identical(self):
        first = knowledge_record()
        first['page']['object'] = 'page'
        first['page']['request_id'] = 'req-00000001'
        second = copy.deepcopy(first)
        second['page']['request_id'] = 'req-00000002'
        a, b = self.origin(first), self.origin(second)
        self.assertNotEqual(a['content_sha256'], b['content_sha256'])  # the read did differ
        self.assertEqual(a['semantic_sha256'], b['semantic_sha256'])
        self.assertEqual(a['render_sha256'], b['render_sha256'])
        # render_digest blanks content_sha256 once a semantic_sha256 is present, so the two
        # reads agree on the identity even though the recorded digest of the read differs.
        self.assertEqual(render_digest(render(first)), render_digest(render(second)))

    def test_a_real_page_edit_is_still_a_change(self):
        first = knowledge_record()
        first['page']['object'] = 'page'
        first['page']['request_id'] = 'req-00000001'
        edited = copy.deepcopy(first)
        edited['page']['request_id'] = 'req-00000002'
        edited['blocks'][1]['paragraph'].update(rich_text=[rt('あとから直した本文')])
        self.assertNotEqual(self.origin(first)['semantic_sha256'],
                            self.origin(edited)['semantic_sha256'])
        self.assertNotEqual(self.origin(first)['render_sha256'],
                            self.origin(edited)['render_sha256'])


class RepeatSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'sync-state'
        self.config = self.root / 'sync.yaml'
        self.config.write_text(yaml.safe_dump(
            {'scope': 'demo', 'selection_id': 'repeat', 'sources': [SOURCE]}, allow_unicode=True))
        shutil.copy(EXAMPLE_CONFIG, self.root / 'import.yaml')
        self.record = knowledge_record()
        self.transport = Transport(self.record)

    def run_sync(self, **kwargs):
        return sync(self.config, self.vault, self.state, fetch=self.transport, **kwargs)

    def note(self):
        return (self.vault / '20-sources' / (identifier(SOURCE) + '.md')).read_bytes()

    def test_repeating_a_sync_of_an_unchanged_page_writes_nothing_new(self):
        self.assertEqual([s['status'] for s in self.run_sync()['sources']], ['created'])
        before = self.note()
        for _ in range(2):
            result = self.run_sync()
            self.assertTrue(result['complete'])
            self.assertEqual([s['status'] for s in result['sources']], ['unchanged'])
            self.assertEqual(self.note(), before)
        # The reads really were distinct: a new request_id and a new signed URL each time.
        self.assertGreater(len(self.transport.calls), 3)

    def test_an_edited_page_is_revised_on_the_next_sync(self):
        self.run_sync()
        before = self.note()
        self.record['blocks'][1]['paragraph'].update(rich_text=[rt('あとから直した本文')])
        self.assertEqual([s['status'] for s in self.run_sync()['sources']], ['revised'])
        self.assertNotEqual(self.note(), before)


if __name__ == '__main__':
    unittest.main()

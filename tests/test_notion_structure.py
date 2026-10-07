"""Tables are checked by parsing the generated HTML, not matching the renderer's strings."""
import copy
import json
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from knowledge_memory.core import MemoryError, parse_note
from knowledge_memory.notion_blocks import blocks_to_markdown
from knowledge_memory.notion_import import import_snapshot
from knowledge_memory.notion_semantics import stable_record
from knowledge_memory.staging import render_digest
from test_knowledge_import import CONFIG, MANIFEST, PAGE_ID, knowledge_record, render, rt


class TableReader(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows = []; self.headers = []; self.links = []
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr': self.rows.append([])
        if tag in ('td', 'th'):
            self.cell = ''
            self.headers.append((tag, dict(attrs).get('scope')))
        if tag == 'br': self.cell += '\n'
        if tag == 'a': self.links.append(dict(attrs)['href'])

    def handle_data(self, text):
        if self.cell is not None: self.cell += text

    def handle_endtag(self, tag):
        if tag in ('td', 'th'):
            self.rows[-1].append(self.cell); self.cell = None


def table(columns=True, rows=False):
    return {'id': 'table-one', 'type': 'table',
            'table': {'table_width': 2, 'has_column_header': columns, 'has_row_header': rows},
            '_children': [{'type': 'table_row', 'table_row': {'cells': [[rt(a)], [rt(b)]]}}
                          for a, b in [('日本語|見出し', ''), ('A\nB <tag>', '本文')]]}


def attachment():
    return {'id': '12345678-abcd-abcd-abcd-123456789abc', 'type': 'file',
            'file': {'type': 'file', 'name': '控え.md', 'caption': [rt('保存時点の控え')],
                     'file': {'url': 'https://files.example.org/object-1/notes.md?X-Amz-Signature=old&X-Amz-Date=old',
                              'expiry_time': '2026-01-01T00:00:00Z'}}}


class NotionStructureTests(unittest.TestCase):
    def test_table_matrix_headers_empty_cells_and_links(self):
        for columns, rows in ((False, False), (True, False), (False, True), (True, True)):
            block = table(columns, rows)
            cell = block['_children'][1]['table_row']['cells'][1][0]
            cell['text']['link'] = {'url': 'https://example.org/?a=1&b=2'}
            out = blocks_to_markdown([block, copy.deepcopy(block)])
            parsed = TableReader(); parsed.feed(out)
            self.assertEqual(parsed.rows, [['日本語|見出し', ''], ['A\nB <tag>', '本文']] * 2)
            self.assertEqual(parsed.links, ['https://example.org/?a=1&b=2'] * 2)
            expected = [('th', 'col') if columns else ('th', 'row') if rows else ('td', None),
                        ('th', 'col') if columns else ('td', None),
                        ('th', 'row') if rows else ('td', None), ('td', None)]
            self.assertEqual(parsed.headers, expected * 2)

    def test_invalid_width_is_not_silently_repaired(self):
        block = table(); block['table']['table_width'] = 3
        with self.assertRaises(MemoryError): blocks_to_markdown([block])

    def test_attachment_keeps_stable_reference_without_duplicate_body(self):
        record = knowledge_record(); record['blocks'].append(attachment())
        note = parse_note('test', render(record).encode())
        self.assertEqual(note.body.count('根拠として保持する本文。'), 1)
        self.assertIn('控え.md', note.body)
        self.assertIn('保存時点の控え', note.body)
        self.assertIn(f'https://www.notion.so/{PAGE_ID}#12345678abcdabcdabcd123456789abc', note.body)
        self.assertNotIn('X-Amz', note.content)
        self.assertNotIn('files.example.org', note.content)

    def test_signed_url_only_keeps_note_and_raw_evidence_distinct(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); snapshot = root / 'snapshot'; snapshot.mkdir()
            (snapshot / 'manifest.json').write_text(json.dumps(MANIFEST))
            record = knowledge_record(); record['blocks'].append(attachment())
            path = snapshot / 'record.json'; path.write_text(json.dumps(record))
            kwargs = dict(snapshot=snapshot, vault=root / 'vault', scope='demo', import_config=CONFIG)
            first = import_snapshot(**kwargs)
            self.assertNotIn('semantic_sha256', first)  # per-record, never an aggregate hash
            note_path = root / 'vault' / '20-sources' / f'src-{PAGE_ID}.md'
            before = note_path.read_bytes()
            changed = copy.deepcopy(record)
            changed['blocks'][-1]['file']['file']['url'] = changed['blocks'][-1]['file']['file']['url'].replace('old', 'new')
            changed['blocks'][-1]['file']['file']['expiry_time'] = '2026-02-01T00:00:00Z'
            path.write_text(json.dumps(changed))
            self.assertEqual(import_snapshot(**kwargs)['unchanged'], first['created'])
            self.assertEqual(before, note_path.read_bytes())
            a = parse_note('a', render(record).encode()).metadata['origin']
            b = parse_note('b', render(changed).encode()).metadata['origin']
            self.assertNotEqual(a['content_sha256'], b['content_sha256'])
            self.assertEqual(a['semantic_sha256'], b['semantic_sha256'])
            self.assertEqual(a['render_sha256'], b['render_sha256'])
            self.assertEqual(first['conversion_reports']['src-' + PAGE_ID][0]['action'], 'attachment_reference_only')

    def test_semantic_changes_are_not_hidden(self):
        record = knowledge_record(); record['blocks'] += [table(), attachment()]
        baseline = render_digest(render(record))
        changes = [
            lambda r: r['blocks'][1]['paragraph'].update(rich_text=[rt('変更本文')]),
            lambda r: r['blocks'][2]['_children'][0]['table_row'].update(cells=[[rt('変更セル')], []]),
            lambda r: r['blocks'][-1]['file'].update(name='別の控え.md'),
            lambda r: r['blocks'][-1]['file']['file'].update(url='https://files.example.org/object-2/notes.md?X-Amz-Signature=new'),
            lambda r: r['page']['properties']['出典URL'].update(url='https://example.org/other'),
        ]
        for change in changes:
            current = copy.deepcopy(record); change(current)
            self.assertNotEqual(baseline, render_digest(render(current)))

    def test_normalization_is_limited_to_hosted_file_shape(self):
        ordinary = {'type': 'external', 'external': {'url': 'https://example.org/?Signature=one'},
                    'expiry_time': 'content', 'text': {'content': 'X-Amz-Signature=one'}}
        self.assertEqual(stable_record(ordinary), ordinary)
        hosted = attachment()
        hosted['file']['file']['url'] += '&edition=one'
        normalized = stable_record(hosted)
        self.assertIn('edition=one', normalized['file']['file']['url'])
        self.assertIn('expiry_time', hosted['file']['file'])  # raw input was not mutated

    def test_navigation_and_unknown_blocks_are_reported(self):
        report = []
        out = blocks_to_markdown([{'id': 'toc', 'type': 'table_of_contents', 'table_of_contents': {}},
                                  {'id': 'u', 'type': 'future_type', 'future_type': {'data': 'not understood'}}],
                                 report=report)
        self.assertEqual([r['action'] for r in report], ['omit_navigation', 'unsupported'])
        self.assertIn('未対応ブロック: future_type', out)

    def test_code_fence_encloses_internal_backticks_and_following_blocks(self):
        out = blocks_to_markdown([{'type': 'code', 'code': {'language': 'text', 'rich_text': [rt('```\n## 適用条件')]}},
                                  {'type': 'paragraph', 'paragraph': {'rich_text': [rt('後続本文')]}}])
        self.assertTrue(out.startswith('````text\n```\n'))
        self.assertIn('\n````\n\n後続本文', out)

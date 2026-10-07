"""Synthetic Knowledge Database schema; no user pages or source material."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from knowledge_memory.core import MemoryError, build_index, get_note, parse_note, search_notes
from knowledge_memory.notion_config import load_config
from knowledge_memory.notion_import import build, import_snapshot
from knowledge_memory.staging import note_text, finalize

CONFIG = load_config(Path(__file__).parents[1] / 'examples/config/knowledge-import.yaml')
PAGE_ID = 'a' * 32
DS_ID = 'b' * 32
MANIFEST = {'complete': True, 'records_saved': 1, 'data_source_id': DS_ID,
            'database_id': 'c' * 32, 'fetch_finished_at': '2026-09-20T10:00:00Z'}


def rt(value):
    return {'type': 'text', 'text': {'content': value}, 'plain_text': value}


def knowledge_record():
    properties = {
        'Title': {'type': 'title', 'title': [rt('架空教材：栽培研究')]},
        '分野': {'type': 'multi_select', 'multi_select': [{'name': '農学'}, {'name': '計測'}]},
        '種別': {'type': 'select', 'select': {'name': '判断'}},
        '整理状態': {'type': 'select', 'select': {'name': '整理済み'}},
        '出典': {'type': 'rich_text', 'rich_text': [rt('途中まで')]},
        '出典URL': {'type': 'url', 'url': 'https://example.org/research'},
        '資料日': {'type': 'date', 'date': {'start': '2025-02-03', 'end': None, 'time_zone': None}},
        '元ファイル': {'type': 'rich_text', 'rich_text': [rt('架空教材.md')]},
        '関係ない日': {'type': 'date', 'date': {'start': '2000-01-01'}},
        '関係ない分類': {'type': 'multi_select', 'multi_select': [{'name': '混入禁止'}]},
    }
    return {'page': {'id': PAGE_ID, 'url': f'https://www.notion.so/{PAGE_ID}',
                     'created_time': '2025-02-01T10:00:00Z',
                     'last_edited_time': '2026-09-01T10:00:00Z',
                     'parent': {'type': 'data_source_id', 'data_source_id': DS_ID},
                     'properties': properties},
            'properties_full': {'出典': {'object': 'list', 'results': [
                {'type': 'rich_text', 'rich_text': rt('長い説明。' * 600)},
                {'type': 'rich_text', 'rich_text': rt('最後の出典説明')}] }},
            'blocks': [{'id': 'block-1', 'type': 'heading_2',
                        'heading_2': {'rich_text': [rt('研修要約')]}},
                       {'id': 'block-2', 'type': 'paragraph',
                        'paragraph': {'rich_text': [rt('根拠として保持する本文。')]}}]}


def render(record=None, config=None):
    return build(record or knowledge_record(), MANIFEST, 'demo', 'auto', None, None,
                 import_config=config or CONFIG)[-1]


class KnowledgeImportTests(unittest.TestCase):
    def test_mapping_preserves_named_fields_and_distinct_dates(self):
        note = parse_note('test', render().encode())
        m, o = note.metadata, note.metadata['origin']
        self.assertEqual(m['domain'], ['計測', '農学'])
        self.assertNotIn('event_at', m)
        self.assertNotIn('verified_at', m)
        self.assertEqual((m['kind'], m['claim_kind'], m['status']),
                         ('source', 'source_report', 'candidate'))
        self.assertEqual(o['source_metadata']['document_date']['start'], '2025-02-03')
        self.assertTrue(o['source_metadata']['source_description'].endswith('最後の出典説明'))
        self.assertEqual(o['source_metadata']['original_filename'], '架空教材.md')
        self.assertEqual(o['data_source_id'], DS_ID)
        self.assertEqual(o['database_id'], 'c' * 32)
        self.assertEqual(len(m['sources']), 2)
        self.assertEqual(o['source_created_at'], '2025-02-01T10:00:00Z')
        self.assertNotEqual(o['retrieved_at'], o['source_edited_at'])

    def test_every_editorial_state_and_blank_stays_candidate(self):
        for label in ('未整理', '整理中', '整理済み', '見直し', None):
            record = knowledge_record()
            record['page']['properties']['整理状態']['select'] = {'name': label} if label else None
            note = parse_note('test', render(record).encode())
            self.assertEqual(note.metadata['status'], 'candidate')
            self.assertNotIn('verified_at', note.metadata)
            self.assertEqual(note.metadata['origin']['source_metadata']['editorial_status'], label)

    def test_empty_optional_properties_are_explicit_null(self):
        record = knowledge_record()
        record['properties_full'] = {}
        for name in ('種別', '整理状態', '出典', '出典URL', '資料日', '元ファイル'):
            value = record['page']['properties'][name]
            value[value['type']] = [] if value['type'] == 'rich_text' else None
        meta = parse_note('test', render(record).encode()).metadata
        self.assertTrue(all(v is None for v in meta['origin']['source_metadata'].values()))

    def test_typo_or_type_change_does_not_fall_back(self):
        for field, value in [('資料日', {'type': 'rich_text', 'rich_text': []}),
                              ('Title', {'type': 'select', 'select': None})]:
            record = knowledge_record()
            record['page']['properties'][field] = value
            with self.assertRaises(MemoryError):
                render(record)
        config = copy.deepcopy(CONFIG)
        config['properties']['domain'] = 'Typo'
        with self.assertRaises(MemoryError):
            render(config=config)

    def test_schema_rejects_unknown_metadata_and_bad_date(self):
        note = parse_note('test', render().encode())
        for key, value in [('unknown', 'bad'), ('document_date', {'start': 'not-a-date'})]:
            meta = copy.deepcopy(note.metadata)
            meta['origin']['source_metadata'][key] = value
            with self.assertRaises(MemoryError):
                parse_note('bad', note_text(meta, note.body).encode())

    def test_parent_mismatch_and_conflicting_overrides_fail(self):
        record = knowledge_record()
        record['page']['parent']['data_source_id'] = 'd' * 32
        with self.assertRaises(MemoryError):
            render(record)
        with self.assertRaises(MemoryError):
            build(knowledge_record(), MANIFEST, 'demo', 'property', 'Content', None,
                  import_config=CONFIG)

    def test_round_trip_idempotence_config_changes_and_edit_protection(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); snapshot = root / 'snapshot'; snapshot.mkdir()
            vault = root / 'vault'; db = root / 'index.sqlite3'
            (snapshot / 'manifest.json').write_text(json.dumps(MANIFEST))
            record = knowledge_record()
            (snapshot / 'one.json').write_text(json.dumps(record))
            kwargs = dict(snapshot=snapshot, vault=vault, scope='demo', import_config=CONFIG)
            first = import_snapshot(**kwargs)
            self.assertEqual(first['created'], ['src-' + PAGE_ID])
            self.assertEqual(import_snapshot(**kwargs)['unchanged'], first['created'])
            build_index(vault, db)
            self.assertEqual(search_notes(db, '教材', 'demo'), [])
            hit = search_notes(db, '教材', 'demo', include_candidates=True)[0]
            got = get_note(db, hit['id'], 'demo', include_candidates=True)
            self.assertEqual(got['metadata']['origin']['source_metadata']['editorial_status'], '整理済み')
            record['page']['properties']['別分野'] = record['page']['properties']['分野']
            (snapshot / 'one.json').write_text(json.dumps(record))
            changed = copy.deepcopy(CONFIG); changed['properties']['domain'] = '別分野'
            self.assertEqual(import_snapshot(**{**kwargs, 'import_config': changed})['revised'], first['created'])
            path = vault / '20-sources' / f'src-{PAGE_ID}.md'
            note = parse_note(str(path), path.read_bytes())
            note.metadata['status'] = 'active'
            path.write_text(finalize(note.metadata, note.body))
            self.assertEqual(import_snapshot(**kwargs)['promoted'], first['created'])
            path.write_text(path.read_text() + '\n手編集\n')
            self.assertEqual(import_snapshot(**kwargs)['locally_modified'], first['created'])

    def test_render_is_identical_across_python_hash_seeds(self):
        import os
        import subprocess
        import sys
        outputs = []
        for seed in ('1', '2'):
            env = dict(os.environ, PYTHONHASHSEED=seed,
                       PYTHONPATH=str(Path(__file__).parent) + os.pathsep + str(Path(__file__).parents[1] / 'src'))
            outputs.append(subprocess.check_output(
                [sys.executable, '-c', 'from test_knowledge_import import render; print(render())'], env=env))
        self.assertEqual(*outputs)

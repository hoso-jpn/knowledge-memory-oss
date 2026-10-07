import copy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
import urllib.error

import yaml
from knowledge_memory.core import MemoryError, build_index, get_note, read_notes, search_notes
from knowledge_memory.context import build_context
from knowledge_memory.notion_import import import_snapshot
from knowledge_memory.sync import FetchError, GUARD, sync, request
from test_knowledge_import import CONFIG, MANIFEST, DS_ID, knowledge_record, rt
from test_notion_structure import attachment

OTHER_DS = 'd' * 32


def record_for(number, ds=DS_ID):
    record = knowledge_record()
    page_id = f'{number:032x}'
    record['page']['id'] = page_id
    record['page']['url'] = 'https://www.notion.so/' + page_id
    record['page']['object'] = 'page'
    record['page']['parent']['data_source_id'] = ds
    record['page']['properties']['Title']['title'] = [rt(f'架空資料{number}')]
    for i, prop in enumerate(record['page']['properties'].values()):
        prop['id'] = f'prop-{i}'
    return record


class FakeAPI:
    def __init__(self):
        self.records = {f'{i:032x}': record_for(i) for i in (1, 2)}
        self.members = {DS_ID: list(self.records), OTHER_DS: []}
        self.calls, self.fail = [], {}
        self.bad_cursor = False

    def __call__(self, provider, path, method='GET', body=None):
        self.calls.append((provider, method, path, body))
        if path in self.fail: raise self.fail[path]
        parts = urlsplit(path).path.strip('/').split('/')
        if parts[0] == 'data_sources':
            ds = parts[1]
            if len(parts) == 2:
                return {'object': 'data_source', 'id': ds, 'parent': {'database_id': 'c' * 32}}
            assert method == 'POST'
            self.assert_query_body(body)
            start = int(body.get('start_cursor', '0'))
            ids = self.members[ds]
            end = min(start + 100, len(ids))
            return {'results': [copy.deepcopy(self.records[k]['page']) for k in ids[start:end]],
                    'has_more': end < len(ids) or self.bad_cursor,
                    'next_cursor': '100' if self.bad_cursor else str(end) if end < len(ids) else None}
        if parts[0] == 'pages':
            record = self.records[parts[1]]
            if len(parts) == 2: return copy.deepcopy(record['page'])
            for name, value in record['page']['properties'].items():
                if value['id'] == parts[3]:
                    return copy.deepcopy(record['properties_full'].get(name, value))
        if parts[0] == 'blocks':
            return {'results': copy.deepcopy(self.records[parts[1]]['blocks']),
                    'has_more': False, 'next_cursor': None}
        raise AssertionError(path)

    @staticmethod
    def assert_query_body(body):
        assert body['page_size'] == 100
        assert not (body.keys() - {'page_size', 'start_cursor'})  # whole explicit DS; no implicit view filter


class DataSourceSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.vault = self.root / 'vault'; self.state = self.root / 'state'
        self.config = self.root / 'sync.yaml'; self.api = FakeAPI()
        (self.root / 'import.yaml').write_text(yaml.safe_dump(CONFIG, allow_unicode=True))
        self.sources = [{'provider': 'notion', 'data_source_id': DS_ID, 'import_config': 'import.yaml'}]
        self.save()

    def save(self, selection='knowledge', path=None):
        (path or self.config).write_text(yaml.safe_dump({'selection_id': selection, 'scope': 'demo', 'sources': self.sources}))

    def run_sync(self, **kwargs):
        return sync(self.config, self.vault, self.state, fetch=self.api, **kwargs)

    def guard(self): return json.loads((self.vault / GUARD).read_text())

    def test_incomplete_query_preserves_notes_and_blocks_old_index(self):
        self.run_sync()
        db = self.root / 'db.sqlite3'; build_index(self.vault, db)
        before = {p.name: p.read_bytes() for p in self.vault.rglob('*.md')}
        original = self.api
        def incomplete(provider, path, method='GET', body=None):
            result = original(provider, path, method, body)
            if method == 'POST':
                result['request_status'] = {'type': 'incomplete'}
            return result
        self.api = incomplete
        result = self.run_sync()
        self.assertFalse(result['complete'])
        self.assertTrue(all(v['status'] == 'enumeration_failed' for v in self.guard().values()))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.vault.rglob('*.md')})
        self.assertEqual(search_notes(db, '本文', 'demo', include_candidates=True), [])
        journal = json.loads((self.state / 'checkpoint.json').read_text())
        self.assertFalse(next(iter(journal['enumerations'].values()))['complete'])

    def test_initial_repeat_new_and_updated_page_use_same_mapping(self):
        self.assertTrue(self.run_sync()['complete'])
        self.assertEqual([r['status'] for r in self.run_sync()['sources']], ['unchanged', 'unchanged'])
        self.api.records[f'{3:032x}'] = record_for(3)
        self.api.members[DS_ID].append(f'{3:032x}')
        self.api.records[f'{1:032x}']['page']['properties']['整理状態']['select']['name'] = '別状態'
        result = self.run_sync()
        self.assertEqual([r['status'] for r in result['sources']], ['revised', 'unchanged', 'created'])
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 3)
        build_index(self.vault, self.root / 'db.sqlite3')
        got = get_note(self.root / 'db.sqlite3', 'src-' + f'{1:032x}', 'demo', True)
        self.assertEqual(got['metadata']['origin']['data_source_id'], DS_ID)
        self.assertEqual(got['metadata']['status'], 'candidate')

    def test_pagination_over_100_and_cursor_failure(self):
        self.api.records = {f'{i:032x}': record_for(i) for i in range(1, 103)}
        self.api.members[DS_ID] = list(self.api.records)
        result = self.run_sync(dry_run=True)
        self.assertEqual(result['enumerations'][0]['pages'], 102)
        self.assertEqual(len(result['sources']), 102)
        posts = [c for c in self.api.calls if c[1] == 'POST']
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[1][3]['start_cursor'], '100')
        self.api.bad_cursor = True
        self.assertEqual(self.run_sync()['enumerations'][0]['status'], 'enumeration_failed')
        journal = json.loads((self.state / 'checkpoint.json').read_text())
        self.assertFalse(next(iter(journal['enumerations'].values()))['complete'])
        self.assertEqual(list(self.vault.rglob('*.md')), [])

    def test_empty_success_holds_missing_members_and_other_selection_survives(self):
        self.run_sync()
        other_record = record_for(3, OTHER_DS); self.api.records[f'{3:032x}'] = other_record
        self.api.members[OTHER_DS] = [f'{3:032x}']
        other_config = self.root / 'other.yaml'
        self.sources = [{'provider': 'notion', 'data_source_id': OTHER_DS, 'import_config': 'import.yaml'}]
        self.save('blog', other_config)
        sync(other_config, self.vault, self.root / 'other-state', fetch=self.api)
        self.api.members[DS_ID] = []
        result = self.run_sync()
        self.assertEqual(result['enumerations'][0]['pages'], 0)
        self.assertTrue(result['complete'])
        guard = self.guard()
        self.assertEqual(guard['notion:' + f'{1:032x}']['status'], 'missing_from_data_source')
        self.assertEqual(guard['notion:' + f'{3:032x}']['status'], 'current')
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 3)

    def test_failure_holds_old_index_on_all_read_paths(self):
        self.run_sync(); db = self.root / 'db.sqlite3'; build_index(self.vault, db)
        self.api.fail['/data_sources/' + DS_ID] = FetchError('inaccessible')
        result = self.run_sync()
        self.assertFalse(result['complete'])
        self.assertEqual(search_notes(db, '本文', 'demo', include_candidates=True), [])
        self.assertEqual(read_notes(db, 'demo', True, True), [])
        with self.assertRaises(MemoryError): get_note(db, 'src-' + f'{1:032x}', 'demo', True, True)
        self.assertEqual(build_context(db, 'demo', '本文', include_candidates=True)['records'], [])
        self.assertTrue(all(v['status'] == 'inaccessible' for v in self.guard().values()))

    def test_move_archive_and_404_are_distinct_and_preserve_originals(self):
        self.run_sync()
        before = {p.name: p.read_bytes() for p in self.vault.rglob('*.md')}
        self.api.members[DS_ID] = []
        self.api.records[f'{1:032x}']['page']['parent']['data_source_id'] = OTHER_DS
        self.api.records[f'{2:032x}']['page']['archived'] = True
        statuses = {x['status'] for x in self.run_sync()['sources']}
        self.assertEqual(statuses, {'moved', 'archived'})
        self.api.fail['/pages/' + f'{1:032x}'] = FetchError('missing_or_inaccessible')
        self.assertIn('missing_or_inaccessible', {x['status'] for x in self.run_sync()['sources']})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.vault.rglob('*.md')})

    def test_partial_page_failure_and_interrupt_resume_reenumerates(self):
        self.api.fail['/pages/' + f'{2:032x}'] = FetchError('retry_later')
        self.assertFalse(self.run_sync()['complete'])
        self.api.fail.clear(); self.api.calls.clear()
        self.assertTrue(self.run_sync(resume=True)['complete'])
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 2)
        self.assertTrue(any(c[1] == 'POST' for c in self.api.calls))
        self.api.fail['/pages/' + f'{2:032x}'] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.run_sync()
        self.api.fail.clear()
        self.assertTrue(self.run_sync(resume=True)['complete'])
        self.assertEqual(self.guard()['notion:' + f'{2:032x}']['status'], 'current')

    def test_dry_run_does_not_change_any_saved_state(self):
        self.run_sync()
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.api.members[DS_ID] = []
        self.run_sync(dry_run=True)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_duplicate_selectors_deduplicate_or_hold_configuration_conflict(self):
        self.sources.append({'provider': 'notion', 'page_id': f'{1:032x}', 'import_config': 'import.yaml'})
        self.save(); self.run_sync()
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 2)
        self.sources[1] = {'provider': 'notion', 'page_id': f'{1:032x}', 'body_source': 'page'}
        self.save()
        self.assertFalse(self.run_sync()['complete'])
        self.assertEqual(self.guard()['notion:' + f'{1:032x}']['status'], 'configuration_conflict')

    def test_failed_db_cannot_be_bypassed_with_same_page_selector(self):
        self.sources.append({'provider': 'notion', 'page_id': f'{1:032x}', 'import_config': 'import.yaml'})
        self.save(); self.run_sync()
        self.api.fail['/data_sources/' + DS_ID] = FetchError('inaccessible')
        self.run_sync()
        self.assertEqual(self.guard()['notion:' + f'{1:032x}']['status'], 'inaccessible')

    def test_partial_profile_and_state_reuse_are_refused(self):
        self.sources.append({'provider': 'notion', 'data_source_id': OTHER_DS, 'import_config': 'import.yaml'})
        self.save(); self.run_sync()
        self.sources.pop(); self.save()
        with self.assertRaises(MemoryError): self.run_sync()
        self.save('other-profile')
        with self.assertRaises(MemoryError): self.run_sync()

    def test_initial_import_adopted_and_modified_note_not_overwritten(self):
        snapshot = self.root / 'snapshot'; snapshot.mkdir()
        (snapshot / 'manifest.json').write_text(json.dumps(dict(MANIFEST, records_saved=2)))
        for page_id, record in self.api.records.items():
            (snapshot / (page_id + '.json')).write_text(json.dumps(record))
        import_snapshot(snapshot, self.vault, 'demo', import_config=CONFIG)
        note = self.vault / '20-sources' / ('src-' + f'{1:032x}' + '.md')
        note.write_text(note.read_text() + '\n人の編集\n'); before = note.read_bytes()
        result = self.run_sync()
        self.assertIn('locally_modified', {s['status'] for s in result['sources']})
        self.assertEqual(note.read_bytes(), before)
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 2)

    def test_signed_url_only_sync_retains_file_and_saves_new_evidence(self):
        self.api.records[f'{1:032x}']['blocks'].append(attachment())
        self.run_sync(); files = list(self.vault.rglob('*.md'))
        before = {p.name: p.read_bytes() for p in files}
        hosted = self.api.records[f'{1:032x}']['blocks'][-1]['file']['file']
        hosted['url'] = hosted['url'].replace('old', 'new'); hosted['expiry_time'] = '2026-02-01T00:00:00Z'
        self.assertTrue(all(s['status'] == 'unchanged' for s in self.run_sync()['sources']))
        self.assertEqual(before, {p.name: p.read_bytes() for p in files})
        self.assertEqual(len(list((self.state / 'evidence').glob('*.json'))), 3)

    def test_429_retries_same_post_with_body(self):
        error = urllib.error.HTTPError('https://api.notion.com', 429, 'rate limit', {'Retry-After': '0'}, io.BytesIO())
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'{"results":[],"has_more":false,"next_cursor":null}'
        with patch('urllib.request.OpenerDirector.open', side_effect=[error, Response()]) as opened, patch('time.sleep'):
            request('notion', '/data_sources/' + DS_ID + '/query', method='POST', body={'page_size': 100})
        self.assertEqual(opened.call_count, 2)
        for call in opened.call_args_list:
            req = call.args[0]
            self.assertEqual(req.method, 'POST')
            self.assertEqual(json.loads(req.data), {'page_size': 100})

    def test_empty_import_absence_is_scoped_to_source_and_scope(self):
        snapshot = self.root / 'snapshot'; snapshot.mkdir()
        (snapshot / 'manifest.json').write_text(json.dumps(dict(MANIFEST, records_saved=1)))
        (snapshot / 'one.json').write_text(json.dumps(record_for(1)))
        import_snapshot(snapshot, self.vault, 'demo', import_config=CONFIG)
        (snapshot / 'one.json').write_text(json.dumps(record_for(3, OTHER_DS)))
        (snapshot / 'manifest.json').write_text(json.dumps(dict(MANIFEST, records_saved=1, data_source_id=OTHER_DS)))
        result = import_snapshot(snapshot, self.vault, 'demo', import_config=CONFIG)
        self.assertEqual(result['absent_from_snapshot'], [])
        (snapshot / 'one.json').unlink()
        (snapshot / 'manifest.json').write_text(json.dumps(dict(MANIFEST, records_saved=0)))
        result = import_snapshot(snapshot, self.vault, 'demo', import_config=CONFIG)
        self.assertEqual(result['absent_from_snapshot'], ['src-' + f'{1:032x}'])
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 2)

    def test_initial_import_is_held_if_first_enumeration_fails(self):
        snapshot = self.root / 'snapshot'; snapshot.mkdir()
        (snapshot / 'manifest.json').write_text(json.dumps(dict(MANIFEST, records_saved=1)))
        (snapshot / 'one.json').write_text(json.dumps(record_for(1)))
        import_snapshot(snapshot, self.vault, 'demo', import_config=CONFIG)
        db = self.root / 'db.sqlite3'; build_index(self.vault, db)
        self.api.fail['/data_sources/' + DS_ID] = FetchError('missing_or_inaccessible')
        self.assertFalse(self.run_sync()['complete'])
        self.assertEqual(read_notes(db, 'demo', True, True), [])

    def test_single_page_to_db_migration_holds_source_when_enumeration_fails(self):
        self.sources = [{'provider': 'notion', 'page_id': f'{1:032x}', 'import_config': 'import.yaml'}]
        self.save(); self.run_sync()
        self.sources.append({'provider': 'notion', 'data_source_id': DS_ID, 'import_config': 'import.yaml'})
        self.save()
        self.api.fail['/data_sources/' + DS_ID] = FetchError('inaccessible')
        self.run_sync()
        self.assertEqual(self.guard()['notion:' + f'{1:032x}']['status'], 'inaccessible')

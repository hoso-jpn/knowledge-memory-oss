import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import yaml
from knowledge_memory.core import MemoryError, build_index
from knowledge_memory.mcp_server import MemoryTools
from knowledge_memory.profiles import load_profiles, plan

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples/profiles'


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.config = self.root / 'profiles.yaml'
        for p in EXAMPLES.iterdir(): shutil.copy(p, self.root / p.name)
        self.value = yaml.safe_load(self.config.read_text())
        for row in self.value['profiles']:
            for field, suffix in (('vault', 'vault'), ('database', 'index.sqlite3'), ('state', 'state')):
                row[field] = str(self.root / 'data' / row['name'] / suffix)
        self.save()

    def save(self): self.config.write_text(yaml.safe_dump(self.value))

    def test_plan_is_read_only_and_unknown_pages_are_held(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = plan(self.config, self.root / 'inventory.json')
        self.assertEqual(result['held_unselected'], ['notion:' + f'{4:032x}'])
        self.assertEqual((result['network_requests'], result['writes']), (0, 0))
        self.assertFalse(result['live_deployment_verified'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_overlapping_and_symlink_destinations_are_refused(self):
        original = copy.deepcopy(self.value)
        for field in ('vault', 'database', 'state'):
            self.value = copy.deepcopy(original)
            self.value['profiles'][1][field] = self.value['profiles'][0][field]
            self.save()
            with self.assertRaises(MemoryError): load_profiles(self.config)
        self.value = copy.deepcopy(original)
        alias = self.root / 'alias'; alias.symlink_to(Path(original['profiles'][0]['vault']))
        self.value['profiles'][1]['vault'] = str(alias); self.save()
        with self.assertRaises(MemoryError): load_profiles(self.config)

    def test_existing_hardlinked_indexes_are_refused(self):
        import os
        left, right = (Path(row['database']) for row in self.value['profiles'][:2])
        left.parent.mkdir(parents=True); right.parent.mkdir(parents=True)
        left.write_bytes(b'synthetic'); os.link(left, right)
        with self.assertRaises(MemoryError): load_profiles(self.config)

    def test_duplicate_page_and_whole_data_source_are_refused(self):
        path = self.root / 'work.yaml'; config = yaml.safe_load(path.read_text())
        config['sources'][0]['page_id'] = '00000000-0000-0000-0000-000000000001'
        path.write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(MemoryError, 'multiple profiles'): load_profiles(self.config)
        config['sources'][0]['data_source_id'] = config['sources'][0].pop('page_id')
        path.write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(MemoryError, 'exact pages'): load_profiles(self.config)

    def test_scope_mismatch_and_unfixed_candidate_policy_are_refused(self):
        self.value['profiles'][0]['scope'] = 'different'; self.save()
        with self.assertRaises(MemoryError): load_profiles(self.config)
        self.value['profiles'][0]['scope'] = 'blog'
        self.value['profiles'][0]['include_candidates'] = 'yes'; self.save()
        with self.assertRaises(MemoryError): load_profiles(self.config)

    def test_github_repository_case_aliases_cannot_cross_profiles(self):
        for name, repo in (('personal', 'ExampleOrg/Research'), ('work', 'exampleorg/research')):
            path = self.root / (name + '.yaml')
            config = yaml.safe_load(path.read_text())
            config['sources'] = [{'provider': 'github', 'repo': repo,
                                  'path': 'private.md', 'ref': 'main'}]
            path.write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(MemoryError, 'multiple profiles'):
            load_profiles(self.config)
        # Git paths remain case-sensitive, unlike owner/repository names.
        config['sources'][0]['path'] = 'Private.md'
        path.write_text(yaml.safe_dump(config))
        self.assertEqual(len(load_profiles(self.config)), 3)

    def test_separate_vaults_prevent_bidirectional_search_get_context(self):
        profiles = load_profiles(self.config)
        for row in profiles:
            row['vault'].mkdir(parents=True)
            meta = dict(id='note-' + row['name'], title=row['name'], kind='note', status='candidate',
                        scope=row['scope'], claim_kind='source_report', updated='2026-10-02',
                        sources=[{'ref': 'fixture:' + row['name'], 'locator': 'synthetic body'}])
            (row['vault'] / 'note.md').write_text('---\n' + yaml.safe_dump(meta) + '---\n共通語\n')
            build_index(row['vault'], row['database'])
        for row in profiles:
            tools = MemoryTools(row['vault'], row['database'], row['scope'], row['include_candidates'])
            hits = tools.call('search_memory', {'query': '共通語'})
            self.assertEqual([n['id'] for n in hits], ['note-' + row['name']])
            context = tools.call('build_context', {'goal': '共通語'})
            for other in profiles:
                if other['name'] == row['name']: continue
                with self.assertRaises(MemoryError):
                    tools.call('get_memory', {'identifier': 'note-' + other['name']})
                self.assertNotIn('note-' + other['name'], json.dumps(context))
                with self.assertRaises(MemoryError):
                    tools.call('search_memory', {'query': '共通語', 'scope': other['scope']})

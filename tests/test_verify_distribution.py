"""scripts/verify_distribution.py rejects tampered artifacts, built here from src/ without a build backend."""
import base64
import hashlib
import importlib.util
import io
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/verify_distribution.py'


def load():
    spec = importlib.util.spec_from_file_location('verify_distribution', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record_row(name, data):
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
    return f'{name},sha256={digest},{len(data)}\n'


@unittest.skipUnless(SCRIPT.is_file(), 'the verify script ships with the source checkout, not the sdist')
class VerifyDistributionTests(unittest.TestCase):
    def setUp(self):
        self.verify = load()
        self.meta = self.verify.project()
        self.meta['scripts'] = self.meta.get('scripts', {})
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stem = f'knowledge_memory-{self.meta["version"]}'
        self.info = f'{self.stem}.dist-info'

    def requirements(self):
        """Requires-Dist and Provides-Extra lines in the form setuptools writes them."""
        lines = [f'Requires-Dist: {r}\n' for r in self.meta.get('dependencies', [])]
        for extra, requirements in self.meta.get('optional-dependencies', {}).items():
            lines.append(f'Provides-Extra: {extra}\n')
            lines += [f'Requires-Dist: {r}; extra == "{extra}"\n' for r in requirements]
        return ''.join(lines)

    def metadata(self, requirements=None):
        return (f'Metadata-Version: 2.4\nName: {self.meta["name"]}\nVersion: {self.meta["version"]}\n'
                f'License-Expression: Apache-2.0\nLicense-File: LICENSE\n'
                f'Requires-Python: {self.meta["requires-python"]}\n'
                + (self.requirements() if requirements is None else requirements))

    def sources(self):
        return {name: (ROOT / 'src' / name).read_bytes() for name in self.verify.modules()}

    def checkout_tests(self):
        return {name: (ROOT / name).read_bytes() for name in self.verify.test_files()}

    def wheel(self, members, entry_points=None, metadata=None, record=None):
        """A wheel with a correct RECORD unless `record` replaces its text."""
        path = Path(self.temp.name) / f'{self.stem}-py3-none-any.whl'
        scripts = ''.join(f'{k} = {v}\n' for k, v in self.meta['scripts'].items())
        files = list(members) + [
            (f'{self.info}/entry_points.txt', (entry_points or f'[console_scripts]\n{scripts}').encode()),
            (f'{self.info}/METADATA', (metadata or self.metadata()).encode()),
            (f'{self.info}/WHEEL', b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'),
            (f'{self.info}/licenses/LICENSE', (ROOT / 'LICENSE').read_bytes()),
        ]
        if record is None:
            record = ''.join(record_row(n, d) for n, d in files) + f'{self.info}/RECORD,,\n'
        with zipfile.ZipFile(path, 'w') as wheel:
            for name, data in files:
                wheel.writestr(name, data)
            wheel.writestr(f'{self.info}/RECORD', record)
        return path

    def correct_record(self):
        return zipfile.ZipFile(self.wheel(self.sources().items())).read(f'{self.info}/RECORD').decode()

    def sdist(self, members, tests=None, pkg_info=None):
        tests = self.checkout_tests() if tests is None else tests
        members = [('PKG-INFO', (pkg_info or self.metadata()).encode()), *members, *tests.items()]
        members += [(name, (ROOT / name).read_bytes()) for name in ('pyproject.toml', 'README.md', 'LICENSE')]
        path = Path(self.temp.name) / f'{self.stem}.tar.gz'
        with tarfile.open(path, 'w:gz') as sdist:
            for name, data in members:
                member = tarfile.TarInfo(f'{self.stem}/{name}')
                member.size = len(data)
                sdist.addfile(member, io.BytesIO(data))
        return path

    def sources_sdist(self, **kwargs):
        return self.sdist([(f'src/{n}', d) for n, d in self.sources().items()], **kwargs)

    def failures(self, check, path):
        failures = self.verify.Failures()
        check(failures, path, self.meta)
        return '\n'.join(failures)

    def test_artifacts_matching_src_pass(self):
        self.assertEqual(self.failures(self.verify.check_wheel, self.wheel(self.sources().items())), '')
        self.assertEqual(self.failures(self.verify.check_sdist, self.sources_sdist()), '')

    def test_changed_module_content_is_rejected(self):
        files = self.sources()
        files['knowledge_memory/cli.py'] = b'raise SystemExit("tampered")\n'
        self.assertIn('wheel modules differ from src',
                      self.failures(self.verify.check_wheel, self.wheel(files.items())))
        self.assertIn('sdist modules differ from src',
                      self.failures(self.verify.check_sdist, self.sdist([(f'src/{n}', d) for n, d in files.items()])))

    def test_dot_segment_and_duplicate_members_are_rejected(self):
        files = [(f'src/{n}', d) for n, d in self.sources().items()]
        found = self.failures(self.verify.check_sdist, self.sdist(files + [('tests/../../evil.py', b'')]))
        self.assertIn('dot-segment member names', found)
        found = self.failures(self.verify.check_sdist, self.sdist(files + [files[0]]))
        self.assertIn('duplicate members', found)
        with self.assertWarns(UserWarning):  # zipfile warns about the duplicate name itself
            path = self.wheel(list(self.sources().items()) + [('knowledge_memory/cli.py', b'')])
        self.assertIn('duplicate members', self.failures(self.verify.check_wheel, path))

    def test_entry_point_groups_other_than_console_scripts_are_rejected(self):
        scripts = ''.join(f'{k} = {v}\n' for k, v in self.meta['scripts'].items())
        path = self.wheel(self.sources().items(), f'[console_scripts]\n{scripts}[gui_scripts]\nx = y:z\n')
        self.assertIn('entry point groups other than console_scripts',
                      self.failures(self.verify.check_wheel, path))

    # Requires-Dist / Provides-Extra

    def test_requirement_spelling_differences_are_accepted(self):
        lines = self.requirements()
        respelled = (lines.replace('PyYAML==', 'pyyaml == ').replace('extra == "mcp"', "extra=='mcp'")
                     .replace('sentence-transformers', 'Sentence_Transformers'))
        self.assertNotEqual(respelled, lines)
        self.assertEqual(self.failures(self.verify.check_wheel,
                                       self.wheel(self.sources().items(), metadata=self.metadata(respelled))), '')

    def test_requires_dist_mismatch_is_rejected(self):
        lines = self.requirements()
        for changed in (lines.replace('PyYAML==6.0.3', 'PyYAML>=1'),
                        lines.replace('Requires-Dist: PyYAML==6.0.3\n', ''),
                        lines + 'Requires-Dist: requests\n',
                        lines.replace('extra == "mcp"', 'extra == "semantic"')):
            with self.subTest(changed=changed):
                path = self.wheel(self.sources().items(), metadata=self.metadata(changed))
                self.assertIn('Requires-Dist differs from pyproject', self.failures(self.verify.check_wheel, path))
                sdist = self.sources_sdist(pkg_info=self.metadata(changed))
                self.assertIn('Requires-Dist differs from pyproject', self.failures(self.verify.check_sdist, sdist))

    def test_provides_extra_mismatch_is_rejected(self):
        lines = self.requirements()
        for changed in (lines.replace('Provides-Extra: mcp\n', ''), lines + 'Provides-Extra: gpu\n'):
            with self.subTest(changed=changed):
                path = self.wheel(self.sources().items(), metadata=self.metadata(changed))
                self.assertIn('Provides-Extra differs from pyproject', self.failures(self.verify.check_wheel, path))

    def test_unsupported_requirement_forms_fail_with_a_message(self):
        for line in ('Requires-Dist: PyYAML==6.0.3; python_version < "3.12"\n',
                     'Requires-Dist: PyYAML @ https://example.invalid/x.whl\n'):
            with self.subTest(line=line):
                path = self.wheel(self.sources().items(), metadata=self.metadata(line))
                self.assertIn('cannot compare Requires-Dist', self.failures(self.verify.check_wheel, path))

    # RECORD

    def record_failures(self, record):
        return self.failures(self.verify.check_wheel, self.wheel(self.sources().items(), record=record))

    def test_record_hash_mismatch_is_rejected(self):
        record = self.correct_record()
        line = next(r for r in record.splitlines() if r.startswith('knowledge_memory/cli.py,'))
        name, _, size = line.split(',')
        other = record_row(name, b'other bytes').split(',')[1]
        self.assertIn('RECORD hash differs', self.record_failures(record.replace(line, f'{name},{other},{size}')))

    def test_record_size_mismatch_is_rejected(self):
        record = self.correct_record()
        line = next(r for r in record.splitlines() if r.startswith('knowledge_memory/cli.py,'))
        name, digest, size = line.split(',')
        self.assertIn('RECORD size differs', self.record_failures(record.replace(line, f'{name},{digest},{int(size) + 1}')))

    def test_record_missing_duplicate_and_unlisted_entries_are_rejected(self):
        record = self.correct_record()
        line = next(r for r in record.splitlines() if r.startswith('knowledge_memory/cli.py,'))
        self.assertIn('not listed in RECORD', self.record_failures(record.replace(line + '\n', '')))
        self.assertIn('duplicate RECORD entries', self.record_failures(record + line + '\n'))
        self.assertIn('RECORD lists files missing from the wheel',
                      self.record_failures(record + record_row('knowledge_memory/gone.py', b'x')))
        self.assertIn('RECORD entry without a hash',
                      self.record_failures(record.replace(line, 'knowledge_memory/cli.py,,')))
        weak = 'knowledge_memory/cli.py,md5=' + base64.urlsafe_b64encode(
            hashlib.md5((ROOT / 'src/knowledge_memory/cli.py').read_bytes()).digest()).rstrip(b'=').decode()
        self.assertIn('RECORD hash algorithm', self.record_failures(record.replace(line, weak + ',' + line.split(',')[2])))

    def test_record_must_list_itself_without_a_hash(self):
        record = self.correct_record()
        self.assertIn('RECORD must list itself', self.record_failures(record.replace(f'{self.info}/RECORD,,\n', '')))
        self.assertIn('RECORD must list itself without hash or size',
                      self.record_failures(record.replace(f'{self.info}/RECORD,,', f'{self.info}/RECORD,sha256=x,1')))

    def test_malformed_record_is_reported(self):
        for record in ('a,b\n', 'knowledge_memory/cli.py,sha256=abc,not-a-number\n', '"unterminated\n'):
            with self.subTest(record=record):
                self.assertIn('RECORD', self.record_failures(record))

    # sdist tests/

    def test_sdist_tests_must_match_the_checkout(self):
        tests = self.checkout_tests()
        name = sorted(tests)[0]
        changed = dict(tests, **{name: tests[name] + b'\n# changed\n'})
        self.assertIn(f'sdist test files differ from the checkout: [\'{name}\']',
                      self.failures(self.verify.check_sdist, self.sources_sdist(tests=changed)))
        missing = {k: v for k, v in tests.items() if k != name}
        self.assertIn(f'sdist misses test files: [\'{name}\']',
                      self.failures(self.verify.check_sdist, self.sources_sdist(tests=missing)))
        for extra in ('tests/notes.txt', 'tests/private_notes.py', 'tests/__pycache__/x.py'):
            with self.subTest(extra=extra):
                found = self.failures(self.verify.check_sdist, self.sources_sdist(tests=dict(tests, **{extra: b'x'})))
                self.assertIn(f'sdist tests/ has files the checkout does not: [\'{extra}\']', found)

    # installed modules

    def test_installed_module_bytes_must_match_src(self):
        package = Path(self.temp.name) / 'site-packages' / 'knowledge_memory'
        shutil.copytree(ROOT / 'src' / 'knowledge_memory', package,
                        ignore=shutil.ignore_patterns('__pycache__'))
        failures = self.verify.Failures()
        self.verify.check_installed_modules(failures, package)
        self.assertEqual(list(failures), [])
        (package / 'cli.py').write_bytes(b'raise SystemExit("tampered")\n')
        (package / 'extra.py').write_bytes(b'')
        self.verify.check_installed_modules(failures, package)
        found = '\n'.join(failures)
        self.assertIn("installed modules differ from src: ['knowledge_memory/cli.py']", found)
        self.assertIn("installed module set differs from src: ['knowledge_memory/extra.py']", found)

    # malformed metadata files

    def test_malformed_metadata_files_are_failures_not_tracebacks(self):
        scripts = ''.join(f'{k} = {v}\n' for k, v in self.meta['scripts'].items())
        cases = {
            'METADATA without Metadata-Version': dict(metadata=self.metadata().replace('Metadata-Version: 2.4\n', '')),
            'METADATA with a bad version': dict(metadata=self.metadata().replace('2.4', 'two')),
            'entry_points.txt without a section': dict(entry_points='km = x:y\n'),
            'entry_points.txt with a duplicate key': dict(entry_points=f'[console_scripts]\n{scripts}km = x:y\n'),
            'entry_points.txt with %': dict(entry_points=f'[console_scripts]\n{scripts}x = a%(b:c\n'),
            'entry_points.txt with a bad line': dict(entry_points='[console_scripts]\nnot a pair\n'),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                found = self.failures(self.verify.check_wheel, self.wheel(self.sources().items(), **kwargs))
                self.assertTrue(found, label)
        bad = self.metadata().replace('Metadata-Version: 2.4\n', '')
        self.assertIn('PKG-INFO', self.failures(self.verify.check_sdist, self.sources_sdist(pkg_info=bad)))

    def test_unreadable_metadata_bytes_are_reported_briefly(self):
        path = self.wheel(self.sources().items(), metadata='Metadata-Version: 2.4\n')
        broken = Path(self.temp.name) / 'broken.whl'
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(broken, 'w') as dst:
            for name in src.namelist():
                dst.writestr(name, b'\xff\xfe\xfa' if name.endswith('METADATA') else src.read(name))
        broken = broken.replace(path)
        found = self.failures(self.verify.check_wheel, broken)
        self.assertIn('wheel METADATA is malformed (UnicodeDecodeError)', found)
        self.assertNotIn(self.temp.name, found)

    def test_corrupt_archives_are_reported_by_contents(self):
        dist = Path(self.temp.name) / 'dist'
        dist.mkdir()
        (dist / f'{self.stem}-py3-none-any.whl').write_bytes(b'not a zip')
        (dist / f'{self.stem}.tar.gz').write_bytes(b'not a tarball')
        with self.assertRaises(SystemExit) as raised:
            self.verify.contents(type('Args', (), {'dist': str(dist)}))
        self.assertEqual(raised.exception.code, 1)


    def test_artifact_pattern_must_resolve_exactly_once(self):
        first = Path(self.temp.name) / 'one.whl'
        first.write_bytes(b'x')
        self.assertEqual(self.verify.artifact_path(str(first)), first.resolve())
        second = Path(self.temp.name) / 'two.whl'
        second.write_bytes(b'y')
        with self.assertRaises(SystemExit):
            self.verify.artifact_path(str(Path(self.temp.name) / '*.whl'))
        with self.assertRaises(SystemExit):
            self.verify.artifact_path(str(Path(self.temp.name) / '*.tar.gz'))

    def test_console_script_uses_platform_launcher_candidates(self):
        bindir = Path(self.temp.name) / 'bin'
        bindir.mkdir()
        plain = bindir / 'km'
        plain.write_text('', encoding='utf-8')
        self.assertEqual(self.verify.console_script(bindir, 'km'), plain)


    def test_windows_installed_source_comparison_ignores_only_crlf_translation(self):
        self.assertTrue(self.verify.installed_source_matches(b'a\r\nb\r\n', b'a\nb\n', platform='nt'))
        self.assertFalse(self.verify.installed_source_matches(b'a\r\nb\r\n', b'a\nb changed\n', platform='nt'))
        self.assertFalse(self.verify.installed_source_matches(b'a\r\nb\r\n', b'a\nb\n', platform='posix'))


    def test_run_replaces_non_utf8_console_bytes_instead_of_crashing(self):
        failures = self.verify.Failures()
        out = self.verify.run(
            failures,
            [sys.executable, '-c', "import os; os.write(1, b'\\x97usage: ok\\n')"],
            self.temp.name,
            expect='usage:',
        )
        self.assertEqual(list(failures), [])
        self.assertIn('usage:', out)


    def test_log_text_escapes_non_ascii_for_legacy_windows_consoles(self):
        self.assertEqual(self.verify.log_text('search 育種'), r'search \u80b2\u7a2e')


@unittest.skipUnless(SCRIPT.is_file(), 'the workflows ship with the source checkout, not the sdist')
class WorkflowHygieneTests(unittest.TestCase):
    def test_workflows_are_read_only_bounded_and_drop_checkout_credentials(self):
        import yaml
        workflows = sorted((ROOT / '.github/workflows').glob('*.yml'))
        self.assertTrue(workflows)
        for path in workflows:
            with self.subTest(workflow=path.name):
                workflow = yaml.safe_load(path.read_text(encoding='utf-8'))
                self.assertEqual(workflow.get('permissions'), {'contents': 'read'})
                for name, job in workflow['jobs'].items():
                    self.assertNotIn('permissions', job, name)
                    self.assertIsInstance(job.get('timeout-minutes'), int, name)
                    for step in job['steps']:
                        if step.get('uses', '').startswith('actions/checkout@'):
                            self.assertIs(step.get('with', {}).get('persist-credentials'), False, name)


    def test_github_actions_are_pinned_to_commit_shas(self):
        import re
        import yaml
        for path in sorted((ROOT / '.github/workflows').glob('*.yml')):
            workflow = yaml.safe_load(path.read_text(encoding='utf-8'))
            for job in workflow['jobs'].values():
                for step in job['steps']:
                    uses = step.get('uses', '')
                    if uses.startswith('actions/'):
                        ref = uses.rsplit('@', 1)[-1].split()[0]
                        self.assertRegex(ref, re.compile(r'^[0-9a-f]{40}$'), uses)


if __name__ == '__main__':
    unittest.main()

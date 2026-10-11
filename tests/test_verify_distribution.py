"""scripts/verify_distribution.py rejects tampered artifacts, built here from src/ without a build backend."""
import importlib.util
import io
from pathlib import Path
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


@unittest.skipUnless(SCRIPT.is_file(), 'the verify script ships with the source checkout, not the sdist')
class VerifyDistributionTests(unittest.TestCase):
    def setUp(self):
        self.verify = load()
        self.meta = self.verify.project()
        self.meta['scripts'] = self.meta.get('scripts', {})
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stem = f'knowledge_memory-{self.meta["version"]}'

    def metadata(self):
        return (f'Metadata-Version: 2.4\nName: {self.meta["name"]}\nVersion: {self.meta["version"]}\n'
                f'License-Expression: Apache-2.0\nLicense-File: LICENSE\n'
                f'Requires-Python: {self.meta["requires-python"]}\n')

    def sources(self):
        return {name: (ROOT / 'src' / name).read_bytes() for name in self.verify.modules()}

    def wheel(self, members, entry_points=None):
        path = Path(self.temp.name) / f'{self.stem}-py3-none-any.whl'
        info = f'{self.stem}.dist-info'
        scripts = ''.join(f'{k} = {v}\n' for k, v in self.meta['scripts'].items())
        with zipfile.ZipFile(path, 'w') as wheel:
            for name, data in members:
                wheel.writestr(name, data)
            wheel.writestr(f'{info}/entry_points.txt', entry_points or f'[console_scripts]\n{scripts}')
            wheel.writestr(f'{info}/METADATA', self.metadata())
            wheel.writestr(f'{info}/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            wheel.writestr(f'{info}/RECORD', '')
            wheel.writestr(f'{info}/licenses/LICENSE', (ROOT / 'LICENSE').read_bytes())
        return path

    def sdist(self, members):
        members = [('PKG-INFO', self.metadata().encode()), *members]
        members += [(name, (ROOT / name).read_bytes()) for name in ('pyproject.toml', 'README.md', 'LICENSE')]
        path = Path(self.temp.name) / f'{self.stem}.tar.gz'
        with tarfile.open(path, 'w:gz') as sdist:
            for name, data in members:
                member = tarfile.TarInfo(f'{self.stem}/{name}')
                member.size = len(data)
                sdist.addfile(member, io.BytesIO(data))
        return path

    def failures(self, check, path):
        failures = self.verify.Failures()
        check(failures, path, self.meta)
        return '\n'.join(failures)

    def test_artifacts_matching_src_pass(self):
        self.assertEqual(self.failures(self.verify.check_wheel, self.wheel(self.sources().items())), '')
        sdist = self.sdist([(f'src/{n}', d) for n, d in self.sources().items()])
        self.assertEqual(self.failures(self.verify.check_sdist, sdist), '')

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


if __name__ == '__main__':
    unittest.main()

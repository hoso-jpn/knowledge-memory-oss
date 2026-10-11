"""Verify the built wheel and sdist as distributed artifacts, not as a source checkout.

    python -m build
    python scripts/verify_distribution.py contents dist
    python scripts/verify_distribution.py install dist/knowledge_memory-*.whl
    python scripts/verify_distribution.py install dist/knowledge_memory-*.tar.gz

`install` creates a fresh venv outside the checkout, installs only the given artifact and
re-runs this script there as `smoke`. Standard library only; nothing is uploaded anywhere.
CI runs it on Linux only; other platforms are not checked.
"""
from __future__ import annotations

import argparse
import configparser
import email.parser
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'knowledge_memory'
LICENSE_EXPRESSION = 'Apache-2.0'
# dist-info files the wheel must carry; top_level.txt is a setuptools extra that may go away.
WHEEL_INFO = {'METADATA', 'WHEEL', 'RECORD', 'entry_points.txt', 'licenses/LICENSE'}
WHEEL_INFO_OPTIONAL = {'top_level.txt'}
# A source distribution carries setuptools' default tests/*.py as source, but not examples, docs,
# CI config, state or private data. Those tests read examples/ and evaluation/, so they do not run
# from an unpacked sdist on their own; the repository checkout is where the test suite runs.
SDIST_REQUIRED = {'PKG-INFO', 'pyproject.toml', 'README.md', 'LICENSE'}
SDIST_FILES = SDIST_REQUIRED | {'setup.cfg'}
SDIST_EGG_INFO = {'PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'entry_points.txt',
                  'requires.txt', 'top_level.txt'}
SMOKE_VAULT = ROOT / 'examples' / 'vault'
SMOKE_QUERY, SMOKE_ID = '育種', 'note-search-example'


class Failures(list):
    def check(self, ok, message):
        if not ok:
            self.append(message)

    def stop(self, label):
        for message in self:
            print(f'FAIL {label}: {message}', file=sys.stderr)
        if self:
            raise SystemExit(1)

    def exit(self, label):
        self.stop(label)
        print(f'OK {label}')


def project():
    return tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']


def modules():
    src = ROOT / 'src' / PACKAGE
    return {f'{PACKAGE}/{p.relative_to(src).as_posix()}' for p in src.rglob('*.py')
            if '__pycache__' not in p.parts}


def module_bytes(name):
    return (ROOT / 'src' / name).read_bytes()


def check_names(failures, label, names):
    """Member names must be plain relative POSIX paths, each present once."""
    unsafe = sorted(n for n in set(names) if n.startswith('/') or '\\' in n
                    or any(part in ('', '.', '..') for part in n.rstrip('/').split('/')))
    failures.check(not unsafe, f'{label} has absolute, backslash or dot-segment member names: {unsafe}')
    duplicates = sorted({n for n in names if names.count(n) > 1})
    failures.check(not duplicates, f'{label} has duplicate members: {duplicates}')


def check_metadata(failures, label, text, meta):
    msg = email.parser.HeaderParser().parsestr(text)
    failures.check(tuple(map(int, msg['Metadata-Version'].split('.'))) >= (2, 4),
                   f'{label}: Metadata-Version {msg["Metadata-Version"]} cannot carry License-Expression')
    failures.check(msg['Name'] == meta['name'] and msg['Version'] == meta['version'],
                   f'{label}: name/version {msg["Name"]} {msg["Version"]}')
    failures.check(msg['License-Expression'] == LICENSE_EXPRESSION,
                   f'{label}: License-Expression is {msg["License-Expression"]!r}')
    failures.check(msg.get_all('License-File') == ['LICENSE'],
                   f'{label}: License-File is {msg.get_all("License-File")}')
    failures.check(msg['License'] is None and
                   not any(c.startswith('License ::') for c in msg.get_all('Classifier') or []),
                   f'{label}: legacy License field or classifier next to License-Expression')
    failures.check(msg['Requires-Python'] == meta['requires-python'],
                   f'{label}: Requires-Python is {msg["Requires-Python"]}')


def check_wheel(failures, path, meta):
    stem = meta['name'].replace('-', '_')
    failures.check(path.name == f'{stem}-{meta["version"]}-py3-none-any.whl',
                   f'unexpected wheel name or tag: {path.name}')
    info = f'{stem}-{meta["version"]}.dist-info'
    with zipfile.ZipFile(path) as wheel:
        check_names(failures, 'wheel', wheel.namelist())
        names = set(wheel.namelist())
        package = {n for n in names if not n.startswith(f'{info}/')}
        extra = package - modules()
        failures.check(not extra, f'wheel contains files outside the package sources: {sorted(extra)}')
        failures.check(not modules() - package, f'wheel misses modules: {sorted(modules() - package)}')
        changed = sorted(n for n in package & modules() if wheel.read(n) != module_bytes(n))
        failures.check(not changed, f'wheel modules differ from src: {changed}')
        infos = {n.removeprefix(f'{info}/') for n in names - package}
        failures.check(WHEEL_INFO <= infos, f'wheel misses metadata: {sorted(WHEEL_INFO - infos)}')
        failures.check(infos <= WHEEL_INFO | WHEEL_INFO_OPTIONAL,
                       f'unexpected dist-info files: {sorted(infos - WHEEL_INFO - WHEEL_INFO_OPTIONAL)}')
        if WHEEL_INFO <= infos:
            check_metadata(failures, 'wheel METADATA', wheel.read(f'{info}/METADATA').decode(), meta)
            tags = email.parser.HeaderParser().parsestr(wheel.read(f'{info}/WHEEL').decode())
            failures.check(tags.get_all('Tag') == ['py3-none-any'] and tags['Root-Is-Purelib'] == 'true',
                           f'wheel is not pure Python: {tags.get_all("Tag")}')
            failures.check(wheel.read(f'{info}/licenses/LICENSE') == (ROOT / 'LICENSE').read_bytes(),
                           'wheel LICENSE differs from the repository LICENSE')
            entry = configparser.ConfigParser(delimiters=('=',))
            entry.optionxform = str
            entry.read_string(wheel.read(f'{info}/entry_points.txt').decode())
            failures.check(entry.sections() == ['console_scripts'],
                           f'entry point groups other than console_scripts: {entry.sections()}')
            scripts = {k: v.strip() for k, v in entry['console_scripts'].items()} if entry.has_section('console_scripts') else {}
            failures.check(scripts == meta['scripts'], f'console scripts differ from pyproject: {scripts}')


def check_sdist(failures, path, meta):
    stem = meta['name'].replace('-', '_')
    top = f'{stem}-{meta["version"]}'
    failures.check(path.name == f'{top}.tar.gz', f'unexpected sdist name: {path.name}')
    with tarfile.open(path) as sdist:
        members = sdist.getmembers()
        check_names(failures, 'sdist', [m.name for m in members])
        failures.check(all(m.isfile() or m.isdir() for m in members), 'sdist contains links or special files')
        files = {m.name for m in members if m.isfile()}
        outside = {n for n in files if not n.startswith(f'{top}/')}
        failures.check(not outside, f'sdist files outside {top}/: {sorted(outside)}')
        rel = {n.removeprefix(f'{top}/') for n in files - outside}
        src = {n.removeprefix('src/') for n in rel if n.startswith(f'src/{PACKAGE}/')}
        egg = {n.removeprefix(f'src/{PACKAGE}.egg-info/') for n in rel if n.startswith(f'src/{PACKAGE}.egg-info/')}
        tests = {n for n in rel if n.startswith('tests/')}
        failures.check(src == modules(), f'sdist package sources differ: {sorted(src ^ modules())}')
        changed = sorted(n for n in src & modules()
                         if sdist.extractfile(f'{top}/src/{n}').read() != module_bytes(n))
        failures.check(not changed, f'sdist modules differ from src: {changed}')
        failures.check(egg <= SDIST_EGG_INFO, f'unexpected egg-info files: {sorted(egg - SDIST_EGG_INFO)}')
        failures.check(all(Path(n).suffix == '.py' and '__pycache__' not in n for n in tests),
                       f'sdist tests/ contains non-source files: {sorted(n for n in tests if not n.endswith(".py"))}')
        failures.check(SDIST_REQUIRED <= rel, f'sdist misses {sorted(SDIST_REQUIRED - rel)}')
        unexpected = rel - SDIST_FILES - tests - {f'src/{n}' for n in src} - {f'src/{PACKAGE}.egg-info/{n}' for n in egg}
        failures.check(not unexpected, f'unexpected sdist files: {sorted(unexpected)}')
        if 'PKG-INFO' in rel:
            check_metadata(failures, 'sdist PKG-INFO', sdist.extractfile(f'{top}/PKG-INFO').read().decode(), meta)
        if 'LICENSE' in rel:
            failures.check(sdist.extractfile(f'{top}/LICENSE').read() == (ROOT / 'LICENSE').read_bytes(),
                           'sdist LICENSE differs from the repository LICENSE')


def contents(args):
    meta = project()
    meta['scripts'] = meta.get('scripts', {})
    dist = Path(args.dist)
    found = sorted(p.name for p in dist.iterdir())
    wheels, sdists = sorted(dist.glob('*.whl')), sorted(dist.glob('*.tar.gz'))
    failures = Failures()
    failures.check(len(wheels) == 1 and len(sdists) == 1 and len(found) == 2,
                   f'expected exactly one wheel and one sdist in {dist}, found {found}')
    if wheels:
        check_wheel(failures, wheels[0], meta)
    if sdists:
        check_sdist(failures, sdists[0], meta)
    failures.exit(f'contents of {", ".join(found)}')


def install(args):
    artifact = Path(args.artifact).resolve()
    if not artifact.name.endswith(('.whl', '.tar.gz')):
        raise SystemExit(f'not a wheel or sdist: {artifact}')
    with tempfile.TemporaryDirectory(prefix='km-dist-') as tmp:
        tmp = Path(tmp)
        if tmp.is_relative_to(ROOT):
            raise SystemExit(f'temporary directory must be outside the checkout: {tmp}')
        # symlinks keep interpreters that are themselves symlinks (e.g. uv-managed) able to find their stdlib.
        venv.create(tmp / 'venv', with_pip=True, symlinks=True)
        python = tmp / 'venv' / 'bin' / 'python'
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
        subprocess.run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                        str(artifact)], check=True, cwd=tmp, env=env)
        # -I keeps PYTHONPATH, user site-packages and this script's directory out of sys.path.
        result = subprocess.run([python, '-I', str(Path(__file__).resolve()), 'smoke', str(artifact)],
                                cwd=tmp, env=env)
        raise SystemExit(result.returncode)


def run(failures, command, cwd, expect=None):
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8')
    shown = ' '.join(str(Path(c).name) if i == 0 else str(c) for i, c in enumerate(command))
    failures.check(result.returncode == 0, f'{shown} exited {result.returncode}: {result.stderr.strip()[-500:]}')
    if expect is not None:
        failures.check(expect in result.stdout, f'{shown} output lacks {expect!r}')
    print(f'ran {shown}: exit {result.returncode}')
    return result.stdout


def smoke(args):
    artifact = Path(args.artifact)
    meta = project()
    label = f'{artifact.name} on Python {sys.version.split()[0]}'
    failures = Failures()
    failures.check(sys.prefix != sys.base_prefix, 'smoke must run inside the venv that holds the artifact')
    failures.check('PYTHONPATH' not in os.environ, 'PYTHONPATH is set')
    failures.check(not Path.cwd().resolve().is_relative_to(ROOT), f'cwd {Path.cwd()} is inside the checkout')
    failures.stop(label)

    import knowledge_memory
    location = Path(knowledge_memory.__file__).resolve()
    purelib = Path(sysconfig.get_path('purelib')).resolve()
    print(f'imported {PACKAGE} from {location}')
    failures.check(location.is_relative_to(purelib), f'{PACKAGE} imported from {location}, not {purelib}')
    failures.check(not location.is_relative_to(ROOT / 'src'), f'{PACKAGE} imported from the checkout: {location}')
    installed = {f'{PACKAGE}/{p.relative_to(location.parent).as_posix()}'
                 for p in location.parent.rglob('*.py') if '__pycache__' not in p.parts}
    failures.check(installed == modules(), f'installed modules differ from src: {sorted(installed ^ modules())}')

    dist = importlib.metadata.distribution(meta['name'])
    failures.check(dist.version == meta['version'], f'installed version {dist.version}')
    origin = json.loads(dist.read_text('direct_url.json') or '{}')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    failures.check(origin.get('url', '').endswith('/' + artifact.name) and 'dir_info' not in origin,
                   f'installed from {origin.get("url")}, not {artifact.name}')
    failures.check(origin.get('archive_info', {}).get('hashes', {}).get('sha256') == digest,
                   f'installed archive hash differs from {artifact.name}')
    failures.check(dist.read_text('licenses/LICENSE') == (ROOT / 'LICENSE').read_text(encoding='utf-8'),
                   'installed license file differs from the repository LICENSE')
    failures.check(dist.metadata['License-Expression'] == LICENSE_EXPRESSION,
                   f'installed License-Expression is {dist.metadata["License-Expression"]!r}')
    failures.stop(label)

    bindir = Path(sysconfig.get_path('scripts'))
    with tempfile.TemporaryDirectory(prefix='km-smoke-') as tmp:
        run(failures, [sys.executable, '-I', '-m', PACKAGE, '--help'], tmp, expect='usage:')
        for name in meta.get('scripts', {}):
            script = bindir / name
            failures.check(script.is_file(), f'console script {name} is not installed in {bindir}')
            if script.is_file():
                run(failures, [script, '--help'], tmp, expect='usage:')
        km, db = bindir / 'km', Path(tmp) / 'smoke.sqlite3'
        run(failures, [km, 'validate', '--vault', SMOKE_VAULT], tmp, expect='"valid": true')
        run(failures, [km, 'index', '--vault', SMOKE_VAULT, '--db', db], tmp, expect='"indexed"')
        run(failures, [km, 'search', '--db', db, '--scope', 'demo', SMOKE_QUERY], tmp, expect=f'"id": "{SMOKE_ID}"')
        run(failures, [km, 'get', '--db', db, '--scope', 'demo', SMOKE_ID], tmp, expect=f'"id": "{SMOKE_ID}"')
    failures.exit(label)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('contents', help='check wheel and sdist files and metadata').add_argument('dist')
    sub.add_parser('install', help='install one artifact into a fresh venv and smoke-test it').add_argument('artifact')
    sub.add_parser('smoke', help='internal: run inside the venv created by install').add_argument('artifact')
    args = parser.parse_args()
    {'contents': contents, 'install': install, 'smoke': smoke}[args.command](args)


if __name__ == '__main__':
    main()

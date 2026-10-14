"""Verify the built wheel and sdist as distributed artifacts, not as a source checkout.

    python -m build
    python scripts/verify_distribution.py contents dist
    python scripts/verify_distribution.py install dist/knowledge_memory-*.whl
    python scripts/verify_distribution.py install dist/knowledge_memory-*.tar.gz

`install` creates a fresh venv outside the checkout, installs only the given artifact and
re-runs this script there as `smoke`. Standard library only; nothing is uploaded anywhere.
CI verifies wheel/sdist installation on Linux and Python 3.11-3.13, plus macOS/Windows on Python 3.13.
Optional-dependency smoke checks install extras without downloading model weights.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import csv
import email.parser
import glob
import hashlib
import importlib.metadata
import io
import json
import os
import re
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
# A source distribution carries the checkout's tests/**/*.py byte for byte (setuptools includes
# them by default), but not examples, docs, CI config, state or private data. Those tests read
# examples/ and evaluation/, so they do not run from an unpacked sdist on their own; the repository
# checkout is where the test suite runs. A test .py that setuptools leaves out is reported as
# missing, so the maintainer decides explicitly whether the sdist should carry it.
SDIST_REQUIRED = {'PKG-INFO', 'pyproject.toml', 'README.md', 'LICENSE'}
SDIST_FILES = SDIST_REQUIRED | {'setup.cfg'}
SDIST_EGG_INFO = {'PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'entry_points.txt',
                  'requires.txt', 'top_level.txt'}
# Wheel spec: RECORD hashes must be sha256 or stronger; md5 and sha1 are not allowed.
RECORD_HASHES = {'sha256', 'sha384', 'sha512', 'sha3_256', 'sha3_384', 'sha3_512', 'blake2b', 'blake2s'}
# Requirements this check can compare without a PEP 508 parser: `name[extras] specifiers`, plus at
# most an `extra == "..."` marker in the metadata. Anything else is reported, not guessed at.
REQUIREMENT = re.compile(r'\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*'
                         r'(?:\[(?P<extras>[A-Za-z0-9._,\s-]*)\])?\s*(?P<spec>[^;@\[\]]*?)\s*'
                         r'(?:;\s*(?P<marker>.*?)\s*)?')
SPECIFIER = re.compile(r'(?:===|~=|==|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+')
EXTRA_MARKER = re.compile(r'extra\s*==\s*(?P<q>[\'"])(?P<extra>[A-Za-z0-9._-]+)(?P=q)')
SMOKE_VAULT = ROOT / 'examples' / 'vault'
SMOKE_QUERY, SMOKE_ID = '育種', 'note-search-example'
EXTRA_DISTRIBUTIONS = {'mcp': 'mcp', 'semantic': 'sentence-transformers'}


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


def test_files():
    tests = ROOT / 'tests'
    return {f'tests/{p.relative_to(tests).as_posix()}' for p in tests.rglob('*.py') if '__pycache__' not in p.parts}


def decoded(failures, label, raw):
    """UTF-8 text of an artifact member, or None after recording a short failure."""
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        failures.check(False, f'{label} is malformed ({type(exc).__name__})')
        return None


def normalized(name):
    """PEP 503 project names and PEP 685 extra names compare case- and separator-insensitively."""
    return re.sub(r'[-_.]+', '-', name).lower()


def requirement(text, extra=None):
    """A comparable form of a simple requirement, or None when the form is not supported."""
    match = REQUIREMENT.fullmatch(text)
    if not match:
        return None
    specs = [s.strip() for s in match['spec'].split(',') if s.strip()]
    if not all(SPECIFIER.fullmatch(s) for s in specs):
        return None
    if match['marker'] is not None:
        marker = EXTRA_MARKER.fullmatch(match['marker'])
        if extra is not None or not marker:
            return None
        extra = marker['extra']
    extras = ','.join(sorted(normalized(e) for e in (match['extras'] or '').split(',') if e.strip()))
    spec = ','.join(sorted(re.sub(r'\s+', '', s) for s in specs))
    return normalized(match['name']), extras, spec, normalized(extra) if extra else ''


def shown(requirements):
    return sorted(f'{n}' + (f'[{x}]' if x else '') + s + (f'; extra == "{e}"' if e else '')
                  for n, x, s, e in requirements)


def check_requirements(failures, label, msg, meta):
    """Requires-Dist and Provides-Extra must say what pyproject.toml says."""
    optional = meta.get('optional-dependencies', {})
    declared = [(r, None) for r in meta.get('dependencies', [])]
    declared += [(r, e) for e, requirements in optional.items() for r in requirements]
    expected = {requirement(r, e) for r, e in declared}
    actual = {requirement(r) for r in msg.get_all('Requires-Dist') or []}
    unsupported = [r for r, e in declared if requirement(r, e) is None] + [
        r for r in msg.get_all('Requires-Dist') or [] if requirement(r) is None]
    if unsupported:
        failures.check(False, f'{label}: cannot compare Requires-Dist {sorted(unsupported)}; this check supports '
                              f'`name[extras] specifiers` with at most an `extra == "..."` marker')
    else:
        failures.check(actual == expected, f'{label}: Requires-Dist differs from pyproject: missing '
                                           f'{shown(expected - actual)}, unexpected {shown(actual - expected)}')
    provided = {normalized(e) for e in msg.get_all('Provides-Extra') or []}
    failures.check(provided == {normalized(e) for e in optional},
                   f'{label}: Provides-Extra differs from pyproject: {sorted(provided)} vs {sorted(optional)}')


def check_names(failures, label, names):
    """Member names must be plain relative POSIX paths, each present once."""
    unsafe = sorted(n for n in set(names) if n.startswith('/') or '\\' in n
                    or any(part in ('', '.', '..') for part in n.rstrip('/').split('/')))
    failures.check(not unsafe, f'{label} has absolute, backslash or dot-segment member names: {unsafe}')
    duplicates = sorted({n for n in names if names.count(n) > 1})
    failures.check(not duplicates, f'{label} has duplicate members: {duplicates}')


def check_metadata(failures, label, raw, meta):
    text = decoded(failures, label, raw)
    if text is None:
        return
    msg = email.parser.HeaderParser().parsestr(text)
    version = msg['Metadata-Version'] or ''
    parsed = tuple(int(p) for p in version.split('.')) if re.fullmatch(r'\d+(\.\d+)*', version) else ()
    failures.check(parsed >= (2, 4),
                   f'{label}: Metadata-Version {version[:20]!r} is missing, invalid or cannot carry License-Expression')
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
    check_requirements(failures, label, msg, meta)


def check_record(failures, wheel, info, names):
    """Every file but RECORD is listed once with a matching hash and size; RECORD lists itself bare."""
    record = f'{info}/RECORD'
    text = decoded(failures, 'wheel RECORD', wheel.read(record))
    if text is None:
        return
    try:
        rows = [row for row in csv.reader(io.StringIO(text, newline=''), strict=True) if row]
    except csv.Error:
        failures.check(False, 'wheel RECORD is malformed (not valid CSV)')
        return
    bad_rows = [i + 1 for i, row in enumerate(rows) if len(row) != 3]
    failures.check(not bad_rows, f'wheel RECORD is malformed: rows {bad_rows[:5]} do not have 3 fields')
    rows = [row for row in rows if len(row) == 3]
    paths = [path for path, _, _ in rows]
    failures.check(len(paths) == len(set(paths)),
                   f'duplicate RECORD entries: {sorted({p for p in paths if paths.count(p) > 1})}')
    files = {n for n in names if not n.endswith('/')}
    failures.check(not files - set(paths), f'wheel files not listed in RECORD: {sorted(files - set(paths))}')
    failures.check(not set(paths) - files, f'RECORD lists files missing from the wheel: {sorted(set(paths) - files)}')
    failures.check(record in paths, 'RECORD must list itself')
    unhashed, algorithms, hashes, sizes = [], [], [], []
    for path, digest, size in rows:
        if path == record:
            failures.check(not digest and not size, 'RECORD must list itself without hash or size')
            continue
        if path not in files:
            continue
        algorithm, _, value = digest.partition('=')
        if not value:
            unhashed.append(path)
            continue
        if algorithm not in RECORD_HASHES:
            algorithms.append(f'{path} ({algorithm[:16]})')
            continue
        data = wheel.read(path)
        expected = base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest()).rstrip(b'=').decode()
        if value != expected:
            hashes.append(path)
        if not size.isdigit() or int(size) != len(data):
            sizes.append(path)
    failures.check(not unhashed, f'RECORD entry without a hash: {unhashed}')
    failures.check(not algorithms, f'RECORD hash algorithm must be sha256 or stronger: {algorithms}')
    failures.check(not hashes, f'RECORD hash differs from the file: {hashes}')
    failures.check(not sizes, f'RECORD size differs from the file or is not a number: {sizes}')


def entry_point_scripts(failures, raw):
    """console_scripts from entry_points.txt, or None after recording why the file cannot be read."""
    text = decoded(failures, 'wheel entry_points.txt', raw)
    if text is None:
        return None
    entry = configparser.ConfigParser(delimiters=('=',), interpolation=None, strict=True)
    entry.optionxform = str
    try:
        entry.read_string(text)
    except configparser.Error as exc:
        failures.check(False, f'wheel entry_points.txt is malformed ({type(exc).__name__})')
        return None
    failures.check(entry.sections() == ['console_scripts'] and not entry.defaults(),
                   f'entry point groups other than console_scripts: {entry.sections()}')
    return {k: v.strip() for k, v in entry['console_scripts'].items()} if entry.has_section('console_scripts') else {}


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
            check_metadata(failures, 'wheel METADATA', wheel.read(f'{info}/METADATA'), meta)
            check_record(failures, wheel, info, names)
            tags = decoded(failures, 'wheel WHEEL', wheel.read(f'{info}/WHEEL'))
            tags = email.parser.HeaderParser().parsestr(tags or '')
            failures.check(tags.get_all('Tag') == ['py3-none-any'] and tags['Root-Is-Purelib'] == 'true',
                           f'wheel is not pure Python: {tags.get_all("Tag")}')
            failures.check(wheel.read(f'{info}/licenses/LICENSE') == (ROOT / 'LICENSE').read_bytes(),
                           'wheel LICENSE differs from the repository LICENSE')
            scripts = entry_point_scripts(failures, wheel.read(f'{info}/entry_points.txt'))
            failures.check(scripts is None or scripts == meta['scripts'],
                           f'console scripts differ from pyproject: {sorted(scripts or [])}')


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
        expected_tests = test_files()
        failures.check(not tests - expected_tests,
                       f'sdist tests/ has files the checkout does not: {sorted(tests - expected_tests)}')
        failures.check(not expected_tests - tests, f'sdist misses test files: {sorted(expected_tests - tests)}')
        changed = sorted(n for n in tests & expected_tests
                         if sdist.extractfile(f'{top}/{n}').read() != (ROOT / n).read_bytes())
        failures.check(not changed, f'sdist test files differ from the checkout: {changed}')
        failures.check(SDIST_REQUIRED <= rel, f'sdist misses {sorted(SDIST_REQUIRED - rel)}')
        unexpected = rel - SDIST_FILES - tests - {f'src/{n}' for n in src} - {f'src/{PACKAGE}.egg-info/{n}' for n in egg}
        failures.check(not unexpected, f'unexpected sdist files: {sorted(unexpected)}')
        if 'PKG-INFO' in rel:
            check_metadata(failures, 'sdist PKG-INFO', sdist.extractfile(f'{top}/PKG-INFO').read(), meta)
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
    for paths, check in ((wheels, check_wheel), (sdists, check_sdist)):
        if not paths:
            continue
        try:
            check(failures, paths[0], meta)
        except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as exc:
            failures.check(False, f'{paths[0].name} cannot be read as an archive ({type(exc).__name__})')
    failures.exit(f'contents of {", ".join(found)}')


def artifact_path(pattern):
    """Resolve exactly one artifact; workflow wildcards are expanded here for cross-platform shells."""
    matches = [Path(p) for p in glob.glob(pattern)]
    if not matches and Path(pattern).is_file():
        matches = [Path(pattern)]
    if len(matches) != 1:
        raise SystemExit(f'expected exactly one artifact matching the supplied path, found {len(matches)}')
    artifact = matches[0].resolve()
    if not artifact.name.endswith(('.whl', '.tar.gz')):
        raise SystemExit(f'not a wheel or sdist: {artifact.name}')
    return artifact


def venv_python(root):
    return root / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def console_script(bindir, name):
    """Return the installed console launcher without assuming POSIX filename rules."""
    candidates = [bindir / name]
    if os.name == 'nt':
        candidates = [bindir / f'{name}.exe', bindir / f'{name}.cmd', bindir / f'{name}.bat', *candidates]
    return next((path for path in candidates if path.is_file()), None)


def install(args):
    artifact = artifact_path(args.artifact)
    if args.extra and args.extra not in EXTRA_DISTRIBUTIONS:
        raise SystemExit(f'unknown extra: {args.extra}')
    with tempfile.TemporaryDirectory(prefix='km-dist-') as tmp:
        tmp = Path(tmp)
        if tmp.is_relative_to(ROOT):
            raise SystemExit(f'temporary directory must be outside the checkout: {tmp}')
        # POSIX symlinks preserve uv-managed interpreters; Windows uses copies to avoid symlink privileges.
        venv.create(tmp / 'venv', with_pip=True, symlinks=(os.name != 'nt'))
        python = venv_python(tmp / 'venv')
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
        target = str(artifact) + (f'[{args.extra}]' if args.extra else '')
        subprocess.run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                        target], check=True, cwd=tmp, env=env)
        # -I keeps PYTHONPATH, user site-packages and this script's directory out of sys.path.
        command = [python, '-I', str(Path(__file__).resolve()), 'smoke', str(artifact)]
        if args.extra:
            command += ['--extra', args.extra]
        result = subprocess.run(command, cwd=tmp, env=env)
        raise SystemExit(result.returncode)


def run(failures, command, cwd, expect=None):
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    shown = ' '.join(str(Path(c).name) if i == 0 else str(c) for i, c in enumerate(command))
    failures.check(result.returncode == 0, f'{shown} exited {result.returncode}: {result.stderr.strip()[-500:]}')
    if expect is not None:
        failures.check(expect in result.stdout, f'{shown} output lacks {expect!r}')
    print(f'ran {shown}: exit {result.returncode}')
    return result.stdout


def installed_source_matches(installed, source, platform=None):
    """Compare Python source bytes, allowing only Git's CRLF checkout translation on Windows."""
    if platform is None:
        platform = os.name
    if platform == 'nt':
        installed = installed.replace(b'\r\n', b'\n')
        source = source.replace(b'\r\n', b'\n')
    return installed == source


def check_installed_modules(failures, package_dir):
    """The installed package holds exactly the src/ modules; Windows ignores checkout CRLF translation."""
    installed = {f'{PACKAGE}/{p.relative_to(package_dir).as_posix()}'
                 for p in package_dir.rglob('*.py') if '__pycache__' not in p.parts}
    failures.check(installed == modules(), f'installed module set differs from src: {sorted(installed ^ modules())}')
    changed = sorted(
        n for n in installed & modules()
        if not installed_source_matches((package_dir.parent / n).read_bytes(), module_bytes(n))
    )
    failures.check(not changed, f'installed modules differ from src: {changed}')


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
    check_installed_modules(failures, location.parent)

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
    if args.extra:
        dependency = EXTRA_DISTRIBUTIONS.get(args.extra)
        try:
            installed_extra_version = importlib.metadata.version(dependency) if dependency else None
        except importlib.metadata.PackageNotFoundError:
            installed_extra_version = None
        failures.check(bool(installed_extra_version), f'extra {args.extra!r} dependency is not installed')
    failures.stop(label)

    bindir = Path(sysconfig.get_path('scripts'))
    with tempfile.TemporaryDirectory(prefix='km-smoke-') as tmp:
        run(failures, [sys.executable, '-I', '-m', PACKAGE, '--help'], tmp, expect='usage:')
        launchers = {}
        for name in meta.get('scripts', {}):
            script = console_script(bindir, name)
            failures.check(script is not None, f'console script {name} is not installed')
            if script is not None:
                launchers[name] = script
                run(failures, [script, '--help'], tmp, expect='usage:')
        km, db = launchers.get('km'), Path(tmp) / 'smoke.sqlite3'
        failures.check(km is not None, 'km console script is not installed')
        failures.stop(label)
        run(failures, [km, 'validate', '--vault', SMOKE_VAULT], tmp, expect='"valid": true')
        run(failures, [km, 'index', '--vault', SMOKE_VAULT, '--db', db], tmp, expect='"indexed"')
        run(failures, [km, 'search', '--db', db, '--scope', 'demo', SMOKE_QUERY], tmp, expect=f'"id": "{SMOKE_ID}"')
        run(failures, [km, 'get', '--db', db, '--scope', 'demo', SMOKE_ID], tmp, expect=f'"id": "{SMOKE_ID}"')
    failures.exit(label)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('contents', help='check wheel and sdist files and metadata').add_argument('dist')
    install_parser = sub.add_parser('install', help='install one artifact into a fresh venv and smoke-test it')
    install_parser.add_argument('artifact', help='artifact path or a pattern that resolves to exactly one file')
    install_parser.add_argument('--extra', choices=sorted(EXTRA_DISTRIBUTIONS))
    smoke_parser = sub.add_parser('smoke', help='internal: run inside the venv created by install')
    smoke_parser.add_argument('artifact')
    smoke_parser.add_argument('--extra', choices=sorted(EXTRA_DISTRIBUTIONS))
    args = parser.parse_args()
    {'contents': contents, 'install': install, 'smoke': smoke}[args.command](args)


if __name__ == '__main__':
    main()

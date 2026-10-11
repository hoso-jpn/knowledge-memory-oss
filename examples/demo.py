"""End-to-end demo on the synthetic vault that ships with this repository.

    python -m pip install -e '.[mcp]'
    python examples/demo.py              # validate, index, search, get, context and MCP
    python examples/demo.py --skip-mcp   # without the [mcp] extra; MCP is reported as skipped
    python examples/demo.py --keep       # keep the temporary workspace to try commands by hand

Every step runs the real `km` CLI or `km-mcp` server against a copy of
examples/context-vault in a temporary directory outside the checkout, checks the result
and prints what it shows. The workspace is removed at the end unless --keep is given.
No network, credential, GPU or model download is used. Exit status 1 means a check failed.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'examples' / 'context-vault'
SCOPE = 'demo'
# The synthetic notes are written in Japanese. 線形 = "linear"; 主張 = "claim";
# the goal reads "resume the yield-forecast review".
QUERY, RECORD = '線形', 'decision-model'
LIFECYCLE_QUERY = '主張'
GOAL, PROJECT = '収量予測の見直しを再開する', 'proj-harvest'
MCP_TIMEOUT = 60


class DemoError(Exception):
    pass


def expect(ok, message):
    if not ok:
        raise DemoError(message)


class Demo:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.vault = workspace / 'vault'
        self.db = workspace / 'index.sqlite3'
        self.step = 0

    def heading(self, title):
        self.step += 1
        print(f'\n== {self.step}. {title}')

    def shown(self, value):
        text = str(value)
        return text.replace(str(self.workspace), '$DEMO')

    def km(self, *args, ok=True, refusal=None):
        command = [sys.executable, '-m', 'knowledge_memory', *map(str, args)]
        print('$ km ' + ' '.join(self.shown(a) for a in args))
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                cwd=self.workspace, timeout=120)
        if ok:
            expect(result.returncode == 0, f'km {args[0]} exited {result.returncode}: {result.stderr.strip()}')
        else:
            expect(result.returncode != 0, f'km {args[0]} was expected to refuse but exited 0')
            expect(refusal is not None and refusal in result.stderr + result.stdout,
                   f'km {args[0]} failed for another reason than {refusal!r}: {result.stderr.strip()}')
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise DemoError(f'km {args[0]} did not print JSON: {result.stdout[:200]!r}') from exc

    def run(self, mcp=True):
        print(f'workspace: {self.workspace} (shown as $DEMO)')
        shutil.copytree(FIXTURE, self.vault)

        self.heading('validate the Markdown records (frontmatter, status, sources)')
        report = self.km('validate', '--vault', self.vault)
        expect(report.get('valid') is True, f'validation failed: {report}')
        print(f'   valid: {report["valid"]}, notes: {report["notes"]}')
        notes = report['notes']

        self.heading('build the SQLite FTS5 index (a rebuildable copy, not the source of truth)')
        report = self.km('index', '--vault', self.vault, '--db', self.db)
        expect(report.get('indexed') == notes, f'unexpected index report: {report}')
        print(f'   indexed: {report["indexed"]}')

        self.heading(f'search active records in scope "{SCOPE}"')
        hits = self.km('search', '--db', self.db, '--scope', SCOPE, QUERY)
        expect(hits and hits[0]['id'] == RECORD, f'expected {RECORD} first, got {[h["id"] for h in hits]}')
        for hit in hits:
            meta = hit['metadata']
            print(f'   {hit["id"]}  status={meta["status"]}  claim_kind={meta["claim_kind"]}  '
                  f'source={meta["sources"][0]["ref"]}')

        self.heading('lifecycle: candidate and retracted records stay out of normal search')
        views = (('default', ()), ('--include-candidates', ('--include-candidates',)),
                 ('--include-candidates --include-history', ('--include-candidates', '--include-history')))
        seen = {}
        for label, flags in views:
            hits = self.km('search', '--db', self.db, '--scope', SCOPE, *flags, LIFECYCLE_QUERY)
            seen[label] = [(h['id'], h['metadata']['status']) for h in hits]
            print(f'   {label}: {seen[label] or "no hits"}')
        expect(all(status == 'active' for _, status in seen['default']),
               f'default search returned non-active records: {seen["default"]}')
        expect(('note-candidate', 'candidate') in seen['--include-candidates'], 'candidate not shown on request')
        expect(('note-withdrawn', 'retracted') in seen['--include-candidates --include-history'],
               'retracted record not shown with --include-history')
        print('   0 hits by default does not mean the information does not exist.')

        self.heading('get the full record with its provenance')
        note = self.km('get', '--db', self.db, '--scope', SCOPE, RECORD)
        expect(note['id'] == RECORD and note['metadata']['sources'], f'unexpected record: {note.get("id")}')
        meta = note['metadata']
        print(f'   id={note["id"]}  path={note["path"]}  status={meta["status"]}  updated={meta["updated"]}  '
              f'event_at={meta["event_at"]}')
        print(f'   sources={meta["sources"]}  sha256={note["sha256"][:16]}...')
        self.sha256 = note['sha256']

        self.heading('build a bounded task context (quotes with sources, no summarising)')
        context = self.workspace / 'context.md'
        report = self.km('context', '--db', self.db, '--scope', SCOPE, '--goal', GOAL, '--project', PROJECT,
                         '--budget', 8000, '--out', context)
        expect(report.get('within_budget') is True and RECORD in report.get('records', []),
               f'unexpected context report: {report}')
        print(f'   size={report["size"]}/{report["budget"]} chars, records={len(report["records"])}, '
              f'dropped for budget={len(report["dropped"])}')
        print('   ' + '\n   '.join(context.read_text(encoding='utf-8').splitlines()[:4]))

        self.heading('a changed source makes the index stale; search refuses until it is rebuilt')
        note_path = self.vault / note['path']
        note_path.write_text(note_path.read_text(encoding='utf-8') + '\n追記\n', encoding='utf-8')
        self.km('search', '--db', self.db, '--scope', SCOPE, QUERY, ok=False,
                refusal='vault changed after indexing; rebuild the index')
        print('   refused: vault changed after indexing; rebuild the index')
        self.km('index', '--vault', self.vault, '--db', self.db)
        hits = self.km('search', '--db', self.db, '--scope', SCOPE, QUERY)
        expect(hits[0]['id'] == RECORD and hits[0]['sha256'] != self.sha256, 'rebuilt index did not pick up the edit')
        print(f'   rebuilt; {RECORD} now has sha256={hits[0]["sha256"][:16]}...')

        self.heading('MCP: start km-mcp over stdio and call it with the official MCP SDK client')
        if not mcp:
            print('   SKIPPED (--skip-mcp). Install the extra and rerun to check MCP.')
            return
        try:
            asyncio.run(asyncio.wait_for(self.mcp(), timeout=MCP_TIMEOUT))
        except TimeoutError as exc:
            raise DemoError(f'MCP step timed out after {MCP_TIMEOUT}s') from exc
        except Exception as exc:  # the SDK reports failures as McpError or an ExceptionGroup
            raise mcp_failure(exc) from exc

    async def mcp(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        args = ['--vault', str(self.vault), '--db', str(self.db), '--scope', SCOPE]
        print('$ km-mcp ' + ' '.join(self.shown(a) for a in args))
        server = StdioServerParameters(command=sys.executable, args=['-m', 'knowledge_memory.mcp_server', *args],
                                       cwd=str(self.workspace))
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = sorted(t.name for t in (await session.list_tools()).tools)
                print(f'   tools/list: {tools}')
                expect(tools == ['build_context', 'get_memory', 'search_memory'], f'unexpected tools: {tools}')

                found = await session.call_tool('search_memory', {'query': QUERY})
                expect(not found.isError, f'search_memory failed: {found.content}')
                hits = json.loads(found.content[0].text)
                print(f'   search_memory {{"query": "{QUERY}"}} -> {[h["id"] for h in hits]}')
                expect(hits and hits[0]['id'] == RECORD, 'MCP search disagrees with the CLI')

                got = await session.call_tool('get_memory', {'identifier': RECORD})
                expect(not got.isError, f'get_memory failed: {got.content}')
                note = json.loads(got.content[0].text)
                print(f'   get_memory {{"identifier": "{RECORD}"}} -> sha256={note["sha256"][:16]}..., '
                      f'sources={note["metadata"]["sources"]}')
                expect(note['sha256'] == hits[0]['sha256'], 'MCP get returned a different record version')

                denied = await session.call_tool('search_memory', {'query': QUERY, 'scope': 'other'})
                print(f'   search_memory with an extra "scope" argument -> refused: {denied.isError}')
                expect(denied.isError, 'the server accepted a scope chosen by the client')
        print('   client session closed; the SDK client ends the server process with it')


def mcp_failure(exc):
    """Return the DemoError inside an SDK exception group, or a readable one for other failures."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    if isinstance(exc, DemoError):
        return exc
    return DemoError(f'MCP step failed: {type(exc).__name__}: {exc} (see km-mcp messages above)')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--skip-mcp', action='store_true', help='skip the MCP step (no [mcp] extra needed)')
    parser.add_argument('--keep', action='store_true', help='keep the temporary workspace and print its path')
    args = parser.parse_args(argv)
    if not args.skip_mcp and importlib.util.find_spec('mcp') is None:
        print("demo: the MCP step needs the extra: python -m pip install -e '.[mcp]' "
              '(or rerun with --skip-mcp)', file=sys.stderr)
        return 2
    if Path(tempfile.gettempdir()).resolve().is_relative_to(ROOT):
        print(f'demo: the temporary directory must be outside the checkout: {tempfile.gettempdir()}',
              file=sys.stderr)
        return 2
    # Turn SIGTERM (e.g. a CI timeout) into SystemExit so the workspace below is still removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    workspace = Path(tempfile.mkdtemp(prefix='km-demo-')).resolve()
    try:
        Demo(workspace).run(mcp=not args.skip_mcp)
    except (DemoError, subprocess.TimeoutExpired, TimeoutError) as exc:
        print(f'\nDEMO FAILED: {exc}', file=sys.stderr)
        return 1
    finally:
        if args.keep:
            print(f'\nkept workspace: {workspace}')
        else:
            shutil.rmtree(workspace, ignore_errors=True)
            print(f'\n{"could not remove" if workspace.exists() else "removed"} workspace: {workspace}')
    print('\nDEMO OK' + (' (MCP skipped)' if args.skip_mcp else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())

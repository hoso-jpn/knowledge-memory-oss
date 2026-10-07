import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from knowledge_memory.core import MemoryError, build_index, search_notes
from knowledge_memory.mcp_server import MemoryTools

FIXTURE = Path(__file__).resolve().parents[1] / 'examples/context-vault'


class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / 'vault'
        shutil.copytree(FIXTURE, self.vault)
        self.db = self.root / 'index.sqlite3'
        build_index(self.vault, self.db)
        self.tools = MemoryTools(self.vault, self.db, 'demo')

    def test_cli_parity_and_source_get(self):
        hits = self.tools.call('search_memory', {'query': '線形'})
        self.assertEqual(hits, search_notes(self.db, '線形', 'demo'))
        note = self.tools.call('get_memory', {'identifier': hits[0]['id']})
        self.assertIn('sources', note['metadata'])
        self.assertEqual(note['sha256'], hits[0]['sha256'])
        context = self.tools.call('build_context', {'goal': '線形', 'budget': 15000})
        self.assertTrue(context['within_budget'])

    def test_authority_cannot_be_widened(self):
        for args in ({'query': 'x', 'scope': 'other'}, {'query': 'x', 'database': '/tmp/x'},
                     {'query': 'x', 'include_history': True}, {'query': 'x', 'limit': True}):
            with self.subTest(args=args), self.assertRaises(MemoryError):
                self.tools.call('search_memory', args)
        for identifier in ('note-candidate', 'note-withdrawn', 'note-other', '../../etc/passwd'):
            with self.assertRaises(MemoryError):
                self.tools.call('get_memory', {'identifier': identifier})
        with self.assertRaises(MemoryError):
            self.tools.call('shell', {'command': 'echo bad'})

    def test_replaced_or_stale_index_is_refused(self):
        (self.vault / '10-notes/state.md').write_text('changed')
        with self.assertRaises(MemoryError):
            self.tools.call('search_memory', {'query': 'x'})

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'install the mcp extra for protocol test')
    def test_real_sdk_stdio_lifecycle(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run():
            parameters = StdioServerParameters(command=sys.executable, args=[
                '-m', 'knowledge_memory.mcp_server', '--vault', str(self.vault),
                '--db', str(self.db), '--scope', 'demo'])
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.assertEqual({t.name for t in listed.tools},
                                     {'search_memory', 'get_memory', 'build_context'})
                    hits = await session.call_tool('search_memory', {'query': '線形'})
                    self.assertFalse(hits.isError)
                    found = json.loads(hits.content[0].text)
                    note = await session.call_tool('get_memory', {'identifier': found[0]['id']})
                    self.assertFalse(note.isError)
                    self.assertEqual(json.loads(note.content[0].text)['sha256'], found[0]['sha256'])
                    context = await session.call_tool('build_context', {'goal': '線形'})
                    self.assertFalse(context.isError)
                    denied = await session.call_tool('search_memory', {'query': 'x', 'scope': 'other'})
                    self.assertTrue(denied.isError)
            # Exiting stdio_client closes stdin and waits/terminates its child.
        asyncio.run(asyncio.wait_for(run(), timeout=30))

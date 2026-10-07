import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from knowledge_memory.bounded import ResponseLimit, chunk_response, response_size
from knowledge_memory.core import MemoryError, build_index, get_note
from knowledge_memory.mcp_server import BoundedMemoryTools, MemoryTools

FIXTURE = Path(__file__).resolve().parents[1] / 'examples/context-vault'


class BoundedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.vault = self.root / 'vault'
        shutil.copytree(FIXTURE, self.vault)
        self.path = self.vault / '10-notes/decision.md'
        self.path.write_text(self.path.read_text() + '\n' + ('引用「日本語😀」\\n\t"\\\n' * 600) +
                             '\n|用語|意味|\n|---|---|\n|末尾|出典fixture:footer|\n')
        self.db = self.root / 'index.sqlite3'; build_index(self.vault, self.db)
        self.tools = BoundedMemoryTools(self.vault, self.db, 'demo', max_chars=1800, max_bytes=2400)

    def checked(self, name, args):
        result = self.tools.call(name, args)
        chars, size = response_size(result)
        self.assertLessEqual(chars, 1800); self.assertLessEqual(size, 2400)
        return result

    def test_lossless_ranges_reach_table_and_footer(self):
        args = {'identifier': 'decision-model'}; fragments = []; end = 0
        for _ in range(200):
            result = self.checked('get_memory_chunk', args)
            self.assertEqual(result['locator']['start'], end)
            end = result['locator']['end']; fragments.append(result['content'])
            self.assertEqual(result['metadata']['status'], 'active')
            if not result['next_cursor']:
                self.assertFalse(result['truncated']); break
            self.assertTrue(result['truncated'])
            args['cursor'] = result['next_cursor']
        else:
            self.fail('continuations failed to finish')
        self.assertEqual(''.join(fragments), self.path.read_text())
        self.assertIn('fixture:footer', fragments[-1])
        self.assertEqual(end, result['locator']['total'])

    def test_version_change_and_stale_index_refuse_continuation(self):
        result = self.checked('get_memory_chunk', {'identifier': 'decision-model'})
        args = {'identifier': 'decision-model', 'cursor': result['next_cursor']}
        self.path.write_text(self.path.read_text() + '\nchanged\n')
        with self.assertRaises(MemoryError): self.tools.call('get_memory_chunk', args)
        build_index(self.vault, self.db)
        with self.assertRaisesRegex(MemoryError, 'changed; search again'):
            self.tools.call('get_memory_chunk', args)
        self.path.unlink()
        with self.assertRaises(MemoryError): self.tools.call('get_memory_chunk', args)

    def test_terminal_chunk_fits_without_continuation_overhead(self):
        vault = self.root / 'tiny-vault'; vault.mkdir()
        raw = ("---\nid: a\ntitle: a\nkind: note\nstatus: active\nscope: demo\n"
               "claim_kind: observation\nupdated: '2026-09-21'\nsources:\n"
               "- ref: a\n  locator: a\n---\nx\n")
        (vault / 'a.md').write_text(raw)
        database = self.root / 'tiny.sqlite3'; build_index(vault, database)
        tools = BoundedMemoryTools(vault, database, 'demo', max_chars=657, max_bytes=657)
        parts, args = [], {'identifier': 'a'}
        for _ in range(len(raw) + 1):
            result = tools.call('get_memory_chunk', args)
            self.assertTrue(tools.response_limit.fits(result))
            parts.append(result['content'])
            if result['next_cursor'] is None: break
            args['cursor'] = result['next_cursor']
        else:
            self.fail('continuation did not reach EOF')
        self.assertEqual(''.join(parts), raw)

    def test_context_search_keeps_valid_budget_above_document_floor(self):
        self.path.write_bytes((FIXTURE / '10-notes/decision.md').read_bytes())
        build_index(self.vault, self.db)
        tools = BoundedMemoryTools(self.vault, self.db, 'demo', max_chars=3000, max_bytes=6000)
        context = tools.call('build_context_bounded', {'goal': '線形', 'project': 'proj-harvest'})
        self.assertTrue(tools.response_limit.fits(context))
        self.assertIn('proj-harvest', context['records'])

    def test_fixed_scope_and_states_and_cursor_validation(self):
        for identifier in ('note-candidate', 'note-withdrawn', 'note-other'):
            with self.assertRaises(MemoryError):
                self.tools.call('get_memory_chunk', {'identifier': identifier})
        for extra in ({'scope': 'other'}, {'cursor': '../path'}, {'cursor': 10}, {'database': 'elsewhere'}):
            with self.assertRaises(MemoryError):
                self.tools.call('get_memory_chunk', dict(identifier='decision-model', **extra))
        candidates = BoundedMemoryTools(self.vault, self.db, 'demo', True)
        self.assertEqual(candidates.call('get_memory_chunk', {'identifier': 'note-candidate'})['metadata']['status'], 'candidate')

    def test_search_and_context_respect_envelope_budget(self):
        result = self.checked('search_memory_bounded', {'query': '線形'})
        self.assertTrue(result['records'])
        with self.assertRaises(MemoryError):
            self.checked('build_context_bounded', {'goal': '線形', 'budget': 60000})
        larger = BoundedMemoryTools(self.vault, self.db, 'demo', max_chars=12000, max_bytes=24000)
        context = larger.call('build_context_bounded', {'goal': '線形', 'budget': 60000})
        self.assertTrue(larger.response_limit.fits(context))
        self.checked('search_memory_bounded', {'query': 'nonexistent-synthetic-term'})

    def test_huge_optional_metadata_is_omitted_but_citation_header_is_not_cut(self):
        note = get_note(self.db, 'decision-model', 'demo')
        note['metadata']['aliases'] = ['巨大' * 10000]
        result = chunk_response(note, None, ResponseLimit(1800, 2400))
        self.assertNotIn('aliases', result['metadata'])
        self.assertEqual(result['metadata']['sources'], note['metadata']['sources'])
        note['metadata']['sources'][0]['locator'] = '巨大' * 10000
        with self.assertRaisesRegex(MemoryError, 'header exceeds'):
            chunk_response(note, None, ResponseLimit(1800, 2400))

    def test_availability_ledger_is_rechecked_between_chunks(self):
        first = self.checked('get_memory_chunk', {'identifier': 'decision-model'})
        (self.vault / '.km-source-status.json').write_text(json.dumps({
            'fixture': {'note_id': 'decision-model', 'status': 'inaccessible'}}))
        with self.assertRaises(MemoryError):
            self.tools.call('get_memory_chunk', {'identifier': 'decision-model', 'cursor': first['next_cursor']})
        hits = self.checked('search_memory_bounded', {'query': '線形'})
        self.assertNotIn('decision-model', [n['id'] for n in hits['records']])

    def test_cli_full_content_and_legacy_adapter_contract_are_unchanged(self):
        legacy = MemoryTools(self.vault, self.db, 'demo')
        self.assertEqual(legacy.call('get_memory', {'identifier': 'decision-model'})['content'], self.path.read_text())
        with self.assertRaises(MemoryError): legacy.call('get_memory_chunk', {'identifier': 'decision-model'})
        with self.assertRaises(MemoryError): self.tools.call('get_memory', {'identifier': 'decision-model'})

    def test_sdk_advertises_only_bounded_contracts_and_returns_bounded_result(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run():
            params = StdioServerParameters(command=sys.executable, args=[
                '-m', 'knowledge_memory.mcp_server', '--vault', str(self.vault), '--db', str(self.db),
                '--scope', 'demo', '--bounded', '--response-max-chars', '1800', '--response-max-bytes', '2400'])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    self.assertEqual({x.name for x in listing.tools}, set(self.tools.tool_names))
                    result = await session.call_tool('get_memory_chunk', {'identifier': 'decision-model'})
                    self.assertFalse(result.isError)
                    wire = result.model_dump_json(by_alias=True, exclude_none=True)
                    self.assertLessEqual(len(wire), 1800)
                    self.assertLessEqual(len(wire.encode()), 2400)
                    self.assertIsNotNone(json.loads(result.content[0].text)['next_cursor'])
                    invalid = await session.call_tool('get_memory_chunk', {'identifier': 'decision-model', 'cursor': '巨大' * 10000})
                    self.assertTrue(invalid.isError)
                    self.assertLessEqual(len(invalid.model_dump_json(exclude_none=True).encode()), 2400)
        asyncio.run(asyncio.wait_for(run(), 30))

"""The evaluation budget must hold at the boundary, across repeats and across tools.

A cap stated only in a prompt can be exceeded, so these tests pin the cases that would make
that possible: the exact boundary, reading the same record twice, spreading calls over
several tools, and asking for a context document instead of a record.

Synthetic throughout: an invented vault, invented notes and no network.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import yaml

from knowledge_memory.core import MemoryError, build_index
from knowledge_memory.eval_budget import (Budget, BudgetExhausted, BudgetedTools,
                                          DOCUMENT_TOOLS, ReportUnwritable, SEARCH_TOOLS,
                                          check_report_path)
from knowledge_memory.mcp_server import MemoryTools

BODY = "ゼル合金の焼成温度は820℃と記録する。架空の数値であり実在しない。" * 12


def note(path: Path, nid: str, title: str, body: str):
    meta = {"id": nid, "title": title, "kind": "note", "status": "active",
            "scope": "budget-demo", "claim_kind": "observation", "updated": "2026-09-01",
            "sources": [{"ref": f"fixture:{nid}", "locator": "架空の記録（実在しません）"}]}
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=10**6)
    path.write_text("---\n" + front + "---\n" + body, encoding="utf-8")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / "vault"; (self.vault / "10-notes").mkdir(parents=True)
        for number in range(3):
            note(self.vault / "10-notes" / f"fx-{number:028d}.md", f"fx-{number:028d}",
                 f"架空ノート{number}", f"# 架空ノート{number}\n\n{BODY}\n")
        self.db = root / "index.sqlite3"
        build_index(self.vault, self.db)
        self.ids = [f"fx-{n:028d}" for n in range(3)]

    def tools(self, searches=6, documents=3, characters=40000, allowed=None):
        budget = Budget(searches, documents, characters)
        inner = MemoryTools(self.vault, self.db, "budget-demo")
        kwargs = {"allowed": allowed} if allowed is not None else {}
        return BudgetedTools(inner, budget, **kwargs), budget

    def size(self, tool, arguments):
        """How many characters one call's payload actually costs."""
        plain = MemoryTools(self.vault, self.db, "budget-demo")
        return len(json.dumps(plain.call(tool, arguments), ensure_ascii=False))


class CountingTests(Fixture):
    def test_counts_are_split_between_searches_and_documents(self):
        api, budget = self.tools()
        api.call("search_memory", {"query": "ゼル合金"})
        api.call("get_memory", {"identifier": self.ids[0]})
        self.assertEqual(budget.spent["searches"], 1)
        self.assertEqual(budget.spent["documents"], 1)
        self.assertGreater(budget.spent["characters"], 0)

    def test_a_context_document_spends_the_document_budget(self):
        """Otherwise the document limit is avoidable by asking for context instead."""
        self.assertEqual(DOCUMENT_TOOLS, {"get_memory", "build_context"})
        self.assertEqual(SEARCH_TOOLS, {"search_memory"})
        api, budget = self.tools(documents=1, characters=10 ** 7)
        api.call("build_context", {"goal": "ゼル合金", "budget": 2000})
        with self.assertRaises(BudgetExhausted):
            api.call("get_memory", {"identifier": self.ids[0]})
        self.assertEqual(budget.spent["documents"], 1)

    def test_reading_the_same_record_again_is_charged_again(self):
        api, budget = self.tools(documents=3, characters=10 ** 7)
        api.call("get_memory", {"identifier": self.ids[0]})
        first = budget.spent["characters"]
        api.call("get_memory", {"identifier": self.ids[0]})
        self.assertEqual(budget.spent["characters"], first * 2)
        self.assertEqual(budget.spent["documents"], 2)


class BoundaryTests(Fixture):
    def test_the_last_allowed_call_succeeds_and_the_next_is_refused(self):
        api, budget = self.tools(searches=2, characters=10 ** 7)
        api.call("search_memory", {"query": "ゼル合金"})
        api.call("search_memory", {"query": "焼成"})
        self.assertEqual(budget.remaining("searches"), 0)
        with self.assertRaises(BudgetExhausted) as caught:
            api.call("search_memory", {"query": "合金"})
        self.assertIn("searches limit 2", str(caught.exception))

    def test_a_payload_of_exactly_the_remaining_characters_fits(self):
        exact = self.size("get_memory", {"identifier": self.ids[0]})
        api, budget = self.tools(characters=exact)
        api.call("get_memory", {"identifier": self.ids[0]})
        self.assertEqual(budget.remaining("characters"), 0)

    def test_a_payload_one_character_over_is_refused_whole(self):
        exact = self.size("get_memory", {"identifier": self.ids[0]})
        api, budget = self.tools(characters=exact - 1)
        with self.assertRaises(BudgetExhausted) as caught:
            api.call("get_memory", {"identifier": self.ids[0]})
        self.assertEqual(budget.spent["characters"], 0)          # nothing was charged
        self.assertIn("nothing was shortened", str(caught.exception))
        self.assertEqual(budget.refusals[0]["exhausted"], "characters")
        self.assertEqual(budget.refusals[0]["needed"], exact)

    def test_a_refused_call_still_spends_its_call_slot(self):
        """Otherwise a caller could retry an oversized read without limit."""
        api, budget = self.tools(documents=3, characters=1)
        for _ in range(3):
            with self.assertRaises(BudgetExhausted):
                api.call("get_memory", {"identifier": self.ids[0]})
        self.assertEqual(budget.spent["documents"], 3)
        with self.assertRaises(BudgetExhausted) as caught:
            api.call("get_memory", {"identifier": self.ids[0]})
        self.assertIn("documents limit 3", str(caught.exception))


class NoQuietTruncationTests(Fixture):
    def test_a_refusal_returns_no_content_at_all(self):
        api, _ = self.tools(characters=1)
        with self.assertRaises(BudgetExhausted) as caught:
            api.call("get_memory", {"identifier": self.ids[0]})
        message = str(caught.exception)
        self.assertNotIn("ゼル合金", message)
        self.assertNotIn(self.ids[0], message)

    def test_what_is_returned_is_never_shorter_than_the_real_record(self):
        api, _ = self.tools(characters=10 ** 7)
        got = api.call("get_memory", {"identifier": self.ids[0]})
        on_disk = (self.vault / "10-notes" / f"{self.ids[0]}.md").read_text(encoding="utf-8")
        self.assertEqual(got["content"], on_disk)


class SpreadAcrossToolsTests(Fixture):
    def test_characters_accumulate_across_all_three_tools(self):
        api, budget = self.tools(searches=6, documents=3, characters=10 ** 7)
        running = 0
        for tool, arguments in (("search_memory", {"query": "ゼル合金"}),
                                ("get_memory", {"identifier": self.ids[0]}),
                                ("build_context", {"goal": "ゼル合金", "budget": 2000})):
            api.call(tool, arguments)
            running += self.size(tool, arguments)
        self.assertEqual(budget.spent["characters"], running)

    def test_the_character_limit_stops_a_sequence_that_each_call_alone_would_fit(self):
        one = self.size("get_memory", {"identifier": self.ids[0]})
        api, budget = self.tools(documents=3, characters=one * 2 + 1)
        api.call("get_memory", {"identifier": self.ids[0]})
        api.call("get_memory", {"identifier": self.ids[1]})
        with self.assertRaises(BudgetExhausted):
            api.call("get_memory", {"identifier": self.ids[2]})
        self.assertEqual(budget.spent["characters"], one * 2)


class ConditionTests(Fixture):
    def test_a_tool_outside_the_condition_is_refused_and_costs_nothing(self):
        api, budget = self.tools(allowed=["get_memory"])
        with self.assertRaises(MemoryError) as caught:
            api.call("search_memory", {"query": "ゼル合金"})
        self.assertIn("not available in this evaluation condition", str(caught.exception))
        self.assertEqual(budget.spent, {"searches": 0, "documents": 0, "characters": 0})

    def test_an_unknown_tool_name_is_rejected_when_the_condition_is_built(self):
        inner = MemoryTools(self.vault, self.db, "budget-demo")
        with self.assertRaises(MemoryError):
            BudgetedTools(inner, Budget(1, 1, 1), ["run_shell"])
        with self.assertRaises(MemoryError):
            BudgetedTools(inner, Budget(1, 1, 1), [])

    def test_a_negative_or_non_integer_budget_is_rejected(self):
        for bad in ((-1, 1, 1), (1, -1, 1), (1, 1, -1), (1.5, 1, 1), (True, 1, 1)):
            with self.assertRaises(MemoryError):
                Budget(*bad)

    def test_the_report_states_limits_spend_and_every_refusal(self):
        api, budget = self.tools(searches=1, characters=10 ** 7)
        api.call("search_memory", {"query": "ゼル合金"})
        with self.assertRaises(BudgetExhausted):
            api.call("search_memory", {"query": "焼成"})
        report = budget.report()
        self.assertEqual(report["limits"]["searches"], 1)
        self.assertEqual(report["spent"]["searches"], 1)
        self.assertEqual(len(report["refusals"]), 1)
        self.assertEqual(report["refusals"][0]["tool"], "search_memory")


class ReportPersistenceTests(Fixture):
    """A client may terminate the server, so the report cannot wait for a clean shutdown."""

    def test_the_report_is_on_disk_after_each_call_including_a_refusal(self):
        path = Path(self.tmp.name) / "spend.json"
        budget = Budget(1, 1, 10 ** 7)
        api = BudgetedTools(MemoryTools(self.vault, self.db, "budget-demo"), budget,
                            report_path=path)
        api.call("search_memory", {"query": "ゼル合金"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["spent"]["searches"], 1)
        with self.assertRaises(BudgetExhausted):
            api.call("search_memory", {"query": "焼成"})
        written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(written["refusals"]), 1)
        self.assertEqual(written["refusals"][0]["exhausted"], "searches")


class DefaultsUnchangedTests(Fixture):
    def test_the_product_tools_are_not_budgeted(self):
        """km-mcp must behave as before; the budget is an evaluation-only wrapper."""
        plain = MemoryTools(self.vault, self.db, "budget-demo")
        for _ in range(20):
            plain.call("search_memory", {"query": "ゼル合金"})
        for _ in range(10):
            plain.call("get_memory", {"identifier": self.ids[0]})
        self.assertFalse(hasattr(plain, "budget"))


if __name__ == "__main__":
    unittest.main()


class ReportDestinationTests(Fixture):
    """The report is rewritten on every call, so a bad destination destroys what is measured."""

    def outside(self, name="spend.json"):
        return Path(self.tmp.name) / name

    def test_a_good_destination_is_accepted(self):
        path = self.outside()
        self.assertEqual(check_report_path(path, self.vault, self.db), path)

    def test_a_path_inside_the_vault_is_refused(self):
        for candidate in (self.vault / "spend.json",
                          self.vault / "10-notes" / f"{self.ids[0]}.md",
                          self.vault / "10-notes" / "deeper" / "spend.json"):
            with self.assertRaises(MemoryError) as caught:
                check_report_path(candidate, self.vault, self.db)
            self.assertIn("vault", str(caught.exception))

    def test_the_index_itself_is_refused(self):
        with self.assertRaises(MemoryError) as caught:
            check_report_path(self.db, self.vault, self.db)
        self.assertIn("index", str(caught.exception))

    def test_a_symlink_is_refused_even_when_it_points_outside(self):
        link = self.outside("link.json")
        link.symlink_to(self.outside("real.json"))
        with self.assertRaises(MemoryError):
            check_report_path(link, self.vault, self.db)

    def test_a_symlink_into_the_vault_is_refused(self):
        link = self.outside("sneaky.json")
        link.symlink_to(self.vault / "10-notes" / f"{self.ids[0]}.md")
        with self.assertRaises(MemoryError):
            check_report_path(link, self.vault, self.db)

    def test_a_hard_link_to_a_vault_note_is_refused(self):
        hard = self.outside("hard.json")
        os.link(self.vault / "10-notes" / f"{self.ids[0]}.md", hard)
        with self.assertRaises(MemoryError) as caught:
            check_report_path(hard, self.vault, self.db)
        self.assertIn("link", str(caught.exception))

    def test_a_missing_parent_directory_is_refused(self):
        with self.assertRaises(MemoryError) as caught:
            check_report_path(self.outside("nope") / "spend.json", self.vault, self.db)
        self.assertIn("parent", str(caught.exception))

    def test_an_unrelated_existing_file_is_refused(self):
        other = self.outside("notes.txt")
        other.write_text("keep me", encoding="utf-8")
        with self.assertRaises(MemoryError):
            check_report_path(other, self.vault, self.db)
        self.assertEqual(other.read_text(encoding="utf-8"), "keep me")

    def test_an_existing_report_may_be_replaced(self):
        path = self.outside()
        path.write_text(json.dumps({"limits": {}, "spent": {}, "refusals": []}), encoding="utf-8")
        self.assertEqual(check_report_path(path, self.vault, self.db), path)


class UnwritableReportTests(Fixture):
    def test_a_call_fails_rather_than_losing_the_report(self):
        api, _ = self.tools()
        api.report_path = Path(self.tmp.name) / "gone" / "spend.json"
        with self.assertRaises(ReportUnwritable) as caught:
            api.call("search_memory", {"query": "ゼル合金"})
        self.assertIn("not valid evidence", str(caught.exception))

    def test_a_written_report_is_replaced_atomically_and_leaves_no_temp_file(self):
        path = Path(self.tmp.name) / "spend.json"
        api, _ = self.tools()
        api.report_path = path
        api.call("search_memory", {"query": "ゼル合金"})
        api.call("get_memory", {"identifier": self.ids[0]})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["spent"]["documents"], 1)
        self.assertEqual([p.name for p in path.parent.glob("*.tmp")], [])


class AdvertisedToolsTests(Fixture):
    def test_the_adapter_advertises_only_the_condition_s_tools(self):
        api, _ = self.tools(allowed=["get_memory"])
        self.assertEqual(api.tool_names, ("get_memory",))
        api, _ = self.tools(allowed=["get_memory", "search_memory"])
        self.assertEqual(api.tool_names, ("search_memory", "get_memory"))

    @unittest.skipUnless(importlib.util.find_spec("mcp"),
                         "install the mcp extra for the protocol test")
    def test_over_the_protocol_a_narrowed_condition_lists_only_its_tools(self):
        from mcp import ClientSession, StdioServerParameters, types
        from mcp.client.stdio import stdio_client

        report = Path(self.tmp.name) / "spend.json"
        parameters = StdioServerParameters(command=sys.executable, args=[
            "-m", "knowledge_memory.eval_budget", "--vault", str(self.vault),
            "--db", str(self.db), "--scope", "budget-demo", "--tools", "get_memory",
            "--budget-report", str(report)])

        async def exercise():
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = sorted(tool.name for tool in listed.tools)
                    got = await session.call_tool("get_memory", {"identifier": self.ids[0]})
                    refused = await session.call_tool("search_memory", {"query": "ゼル合金"})
                    text = "".join(c.text for c in refused.content
                                   if isinstance(c, types.TextContent))
                    return names, got.isError, refused.isError, text

            # Exiting stdio_client closes stdin and waits/terminates its child.

        names, got_error, refused_error, message = asyncio.run(exercise())
        self.assertEqual(names, ["get_memory"])          # not advertised, not just refused
        self.assertFalse(got_error)
        self.assertTrue(refused_error)
        self.assertIn("not available in this evaluation condition", message)
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["spent"]["documents"], 1)


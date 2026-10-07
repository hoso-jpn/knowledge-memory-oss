"""Task context generation, against the synthetic vault in examples/context-vault."""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import yaml

from knowledge_memory.context import (CUT, EMPTY, NO_QUOTE, SECTIONS, build_context,
                                      estimate, place, write_context)
from knowledge_memory.core import MemoryError, build_index

REPOSITORY = Path(__file__).resolve().parent.parent
FIXTURE = REPOSITORY / "examples" / "context-vault"
GOAL = "収量予測の見直しを再開する"


class ContextTestCase(unittest.TestCase):
    def setUp(self):
        if not FIXTURE.is_dir():
            self.skipTest("context fixture is not present")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "context.sqlite3"
        build_index(FIXTURE, self.db)

    def build(self, **kwargs):
        options = dict(scope="demo", goal=GOAL, project="proj-harvest", budget=8000,
                       generated_at="2026-09-22T00:00:00+00:00")
        options.update(kwargs)
        return build_context(self.db, **options)


class SelectionTests(ContextTestCase):
    def test_other_scopes_and_unusual_states_are_left_out(self):
        result = self.build()
        for excluded in ("note-other", "note-withdrawn", "note-candidate"):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, result["records"])
                self.assertNotIn(excluded, result["text"])
        self.assertIn("proj-harvest", result["records"])

    def test_candidates_are_included_only_on_request_and_are_marked(self):
        plain = self.build()
        self.assertNotIn("note-candidate", plain["text"])
        # A candidate reachable from the project only appears when asked for.
        widened = self.build(include_candidates=True, goal="未確認の候補")
        self.assertIn("note-candidate", widened["records"])
        block = re.search(r"(?m)^- \[候補\][^\n]*\n(?:  - [^\n]*\n)+", widened["text"])
        self.assertIsNotNone(block, "a candidate entry should carry the [候補] mark")
        self.assertIn("note-candidate", block.group(0))
        self.assertIn("`[候補]` は未確認の取り込み記録です。確認済みの事実ではありません。",
                      widened["text"])

    def test_a_hypothesis_is_never_presented_as_established(self):
        text = self.build()["text"]
        block = text[text.index("idea-threshold") - 400:text.index("idea-threshold")]
        self.assertIn("[仮説]", block)
        self.assertIn("主張の種類: hypothesis", text)

    def test_links_that_leave_the_selection_are_reported_not_followed(self):
        result = self.build(include_candidates=False)
        # moc-harvest links to idea-threshold and note-cycle, both active, so nothing is
        # skipped here; the reporting keys still exist and stay empty.
        self.assertEqual(result["skipped_outside_scope"], [])
        self.assertEqual(result["skipped_state"], [])

    def test_a_cycle_terminates_and_never_repeats_a_record(self):
        result = self.build(depth=5)
        self.assertIn("note-cycle", result["records"])
        self.assertIn("moc-harvest", result["records"])
        self.assertEqual(len(result["records"]), len(set(result["records"])))
        for identifier in set(result["records"]):
            with self.subTest(identifier=identifier):
                self.assertEqual(result["text"].count(f"記録ID: {identifier} "),
                                 sum(1 for section in SECTIONS
                                     if f"記録ID: {identifier} " in
                                     _section(result["text"], section)))

    def test_depth_limits_how_far_links_are_followed(self):
        shallow = self.build(depth=0)
        deep = self.build(depth=5)
        self.assertEqual(shallow["records"], ["proj-harvest"])
        self.assertLess(len(shallow["records"]), len(deep["records"]))

    def test_the_project_must_be_a_project_record(self):
        with self.assertRaisesRegex(MemoryError, "not project"):
            self.build(project="note-state")
        with self.assertRaisesRegex(MemoryError, "not available"):
            self.build(project="note-other")


class ContradictionTests(ContextTestCase):
    def test_disagreeing_records_are_both_quoted_with_their_sources(self):
        text = self.build()["text"]
        self.assertIn("decision-model", text)
        self.assertIn("note-conflict", text)
        # Both claims survive verbatim; neither is reconciled away.
        self.assertIn("当面は線形モデルを使う。", text)
        self.assertIn("架空の再解析では非線形モデルの誤差が2割小さかった。", text)
        self.assertEqual(text.count("出典: fixture:demo"), text.count("記録ID: "))


class BudgetTests(ContextTestCase):
    def minimum(self, unit="chars"):
        """The smallest budget that is accepted at all.

        The figure the refusal reports is itself printed in the header, so a longer number
        needs a few more characters. Ask again until one is accepted.
        """
        budget = 1
        for _ in range(8):
            try:
                self.build(budget=budget, budget_unit=unit)
                return budget
            except MemoryError as exc:
                found = re.search(r"below the (\d+)", str(exc))
                if not found:
                    raise
                budget = int(found.group(1))
        self.fail("the minimum budget did not settle")

    def first_where(self, predicate, unit="chars"):
        """The smallest workable budget that produces the situation under test."""
        floor = self.minimum(unit)
        for budget in range(floor, floor * 12, max(1, floor // 120)):
            result = self.build(budget=budget, budget_unit=unit)
            if predicate(result):
                return result
        self.fail("no budget in range produced the case under test")

    def test_the_whole_document_stays_within_the_budget(self):
        floor = self.minimum()
        for budget in (floor, floor + 50, floor * 2, floor * 3, floor * 8):
            with self.subTest(budget=budget):
                result = self.build(budget=budget)
                self.assertTrue(result["within_budget"], f"{result['size']} > {budget}")
                self.assertEqual(len(result["text"]), result["size"])
                self.assertLessEqual(result["size"], budget)

    def test_tokens_are_a_separate_unit_and_are_stated(self):
        budget = self.minimum("tokens") * 3
        result = self.build(budget=budget, budget_unit="tokens")
        self.assertTrue(result["within_budget"])
        self.assertLessEqual(estimate(result["text"], "tokens"), budget)
        self.assertIn(f"予算: {budget} tokens（この文書全体の概算。", result["text"])
        # The two units are not interchangeable, and the document names the one in force.
        self.assertNotEqual(estimate(result["text"], "tokens"), len(result["text"]))

    def test_a_trimmed_quotation_says_so(self):
        result = self.first_where(lambda r: r["truncated"])
        self.assertIn(CUT, result["text"])
        self.assertIn("引用を途中で切った記録", result["text"])
        self.assertTrue(result["within_budget"])

    def test_a_record_whose_sources_do_not_fit_is_dropped_whole(self):
        floor = self.minimum()
        for budget in (floor, floor * 2, floor * 3):
            with self.subTest(budget=budget):
                result = self.build(budget=budget)
                for identifier in result["dropped"]:
                    # Dropped means gone, not quoted without its provenance.
                    self.assertNotIn(f"記録ID: {identifier} ", result["text"])
        pinched = self.build(budget=floor)
        self.assertTrue(pinched["dropped"])
        self.assertIn("予算内に収まらず省いた記録", pinched["text"])

    def test_a_record_kept_elsewhere_is_reported_as_a_lost_section(self):
        result = self.first_where(lambda r: r["dropped_sections"])
        for entry in result["dropped_sections"]:
            identifier = entry.split("（")[0]
            with self.subTest(entry=entry):
                self.assertIn(f"記録ID: {identifier} ", result["text"])
                self.assertNotIn(identifier, result["dropped"])
        self.assertIn("予算内に収まらず省いた節", result["text"])

    def test_every_quotation_present_keeps_its_sources(self):
        floor = self.minimum()
        for budget in (floor, floor * 2, floor * 3, floor * 8):
            with self.subTest(budget=budget):
                text = self.build(budget=budget)["text"]
                for block in re.findall(r"(?m)^- (?:\[[^\]]+\])*[^\n]*\n(?:  - [^\n]*\n)+",
                                        text):
                    self.assertIn("出典:", block)

    def test_a_budget_below_the_fixed_parts_is_refused(self):
        # The header and labels cannot be dropped, so such a budget cannot be honoured.
        with self.assertRaisesRegex(MemoryError, "is below the"):
            self.build(budget=1)
        with self.assertRaisesRegex(MemoryError, "before any record is added"):
            self.build(budget=self.minimum() - 1)

    def test_bad_budgets_are_refused(self):
        for kwargs in ({"budget": 0}, {"budget": -5}, {"budget_unit": "words"},
                       {"depth": -1}, {"depth": 99}, {"goal": "  "}, {"scope": "Demo"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MemoryError):
                self.build(**kwargs)


class ReproducibilityTests(ContextTestCase):
    def test_the_same_input_gives_the_same_selection_and_order(self):
        first = self.build()
        second = self.build()
        self.assertEqual(first["text"], second["text"])
        self.assertEqual(first["records"], second["records"])

    def test_only_the_generation_time_differs_between_runs(self):
        first = self.build(generated_at="2026-09-22T00:00:00+00:00")
        later = self.build(generated_at="2027-01-01T09:00:00+00:00")
        strip = lambda text: re.sub(r"(?m)^- 生成時刻: .*$", "- 生成時刻: -", text)
        self.assertNotEqual(first["text"], later["text"])
        self.assertEqual(strip(first["text"]), strip(later["text"]))

    def test_the_order_does_not_depend_on_the_row_order(self):
        rebuilt = self.root / "again.sqlite3"
        build_index(FIXTURE, rebuilt)
        again = build_context(rebuilt, scope="demo", goal=GOAL, project="proj-harvest",
                              budget=8000, generated_at="2026-09-22T00:00:00+00:00")
        self.assertEqual(self.build()["records"], again["records"])


class PresentationTests(ContextTestCase):
    def test_an_empty_section_says_so_rather_than_being_filled_in(self):
        result = self.build(depth=0)
        text = result["text"]
        for section in SECTIONS:
            with self.subTest(section=section):
                self.assertIn(f"## {section}", text)
        self.assertIn(EMPTY, text)

    def test_a_search_with_no_hits_does_not_claim_there_is_no_evidence(self):
        result = self.build(goal="この語句はどの記録にも現れない架空の問い")
        self.assertEqual(result["search_hits"], 0)
        self.assertIn("根拠が存在しないことを意味しません", result["text"])

    def test_the_document_records_how_it_was_made(self):
        result = self.build()
        text = result["text"]
        self.assertIn("生成時刻: 2026-09-22T00:00:00+00:00", text)
        self.assertIn("- scope: demo", text)
        for identifier in result["records"]:
            with self.subTest(identifier=identifier):
                self.assertIn(f"記録ID: {identifier} ", text)
        self.assertEqual(len(re.findall(r"SHA-256: [0-9a-f]{64}", text)),
                         len(re.findall(r"記録ID: ", text)))

    def test_the_output_is_not_a_record(self):
        text = self.build()["text"]
        self.assertFalse(text.startswith("---"))
        self.assertIn("vaultへ戻さないでください", text)


class WriteTests(ContextTestCase):
    def test_writing_inside_the_vault_is_refused(self):
        result = self.build()
        with self.assertRaisesRegex(MemoryError, "outside the vault"):
            write_context(result, FIXTURE / "10-notes" / "context.md", self.db)
        self.assertFalse((FIXTURE / "10-notes" / "context.md").exists())

    def test_writing_outside_the_vault_works(self):
        result = self.build()
        target = self.root / "out" / "context.md"
        write_context(result, target, self.db)
        self.assertEqual(target.read_text(encoding="utf-8"), result["text"])


class CliTests(ContextTestCase):
    def km(self, *args):
        done = subprocess.run([sys.executable, "-m", "knowledge_memory", *args],
                              capture_output=True, text=True)
        return done.returncode, done.stdout, done.stderr

    def test_the_command_reports_json_and_can_print_the_document(self):
        code, out, _ = self.km("context", "--db", str(self.db), "--scope", "demo",
                               "--goal", GOAL, "--project", "proj-harvest",
                               "--budget", "5000")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertTrue(summary["within_budget"])
        self.assertNotIn("text", summary)
        self.assertIn("proj-harvest", summary["records"])

        code, out, _ = self.km("context", "--db", str(self.db), "--scope", "demo",
                               "--goal", GOAL, "--project", "proj-harvest", "--print")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("<!-- このファイルは記録ではありません"))

    def test_failures_are_reported_as_json_with_a_non_zero_exit(self):
        code, out, err = self.km("context", "--db", str(self.db), "--scope", "demo",
                                 "--goal", GOAL, "--project", "note-state")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("not project", json.loads(err)["error"])


def _section(text: str, name: str) -> str:
    start = text.index(f"## {name}")
    rest = text[start + 1:]
    end = re.search(r"(?m)^## |^---$", rest)
    return rest[:end.start()] if end else rest


if __name__ == "__main__":
    unittest.main()


LONG_ID = ("d" + "ecision-with-a-very-long-identifier" * 3 + "-" + "x" * 20)[:128]


class BudgetOverrunTests(ContextTestCase):
    """The finished document is measured, not estimated from a reservation."""

    def test_no_budget_produces_a_document_over_its_own_limit(self):
        # A 128 character id in the trailing lists, repeated per dropped section, is what
        # a reservation used to under-count.
        floor = None
        for budget in range(1200, 6000, 7):
            try:
                result = self.build(budget=budget)
            except MemoryError as exc:
                self.assertIn("is below the", str(exc))
                continue
            floor = floor or budget
            with self.subTest(budget=budget):
                self.assertEqual(result["size"], len(result["text"]))
                self.assertLessEqual(result["size"], budget)
                self.assertTrue(result["within_budget"])
        self.assertIsNotNone(floor, "no budget in the range was accepted")

    def test_the_long_identifier_is_reachable_and_exercised(self):
        result = self.build(budget=20000, depth=1)
        self.assertIn(LONG_ID, result["selected"])

    def test_tokens_are_reported_as_an_estimate_not_a_guarantee(self):
        chars = self.build(budget=20000)
        self.assertEqual(chars["budget_guarantee"], "exact")
        self.assertFalse(chars["size_is_estimate"])
        tokens = self.build(budget=20000, budget_unit="tokens")
        self.assertEqual(tokens["budget_guarantee"], "estimate")
        self.assertTrue(tokens["size_is_estimate"])
        self.assertIn("実際のトークン数はモデル依存で、上限は保証しません", tokens["text"])


class ExcerptTests(ContextTestCase):
    """Nothing in a body is dropped or repeated without saying so."""

    def note(self, identifier):
        from knowledge_memory.core import read_notes
        return {n["metadata"]["id"]: n for n in read_notes(self.db, "demo")}[identifier]

    def test_the_text_before_a_claimed_heading_is_kept(self):
        placed = place(self.note("note-lead"))
        sections = [section for section, _ in placed]
        self.assertIn("現状", sections)
        quoted = "\n".join(text for _, text in placed)
        self.assertIn("ここに前置きがある。落ちてはいけない。", quoted)

    def test_a_sub_heading_stays_inside_its_section(self):
        quoted = "\n".join(text for _, text in place(self.note("note-lead")))
        self.assertIn("下位の内容。落ちてはいけない。", quoted)
        constraint = dict(place(self.note("note-lead")))["制約"]
        self.assertIn("### 下位の条件", constraint)

    def test_a_repeated_heading_is_collected_not_just_the_first(self):
        constraint = dict(place(self.note("note-lead")))["制約"]
        self.assertIn("条件A。", constraint)
        self.assertIn("二度目の適用条件。落ちてはいけない。", constraint)

    def test_a_heading_inside_a_code_fence_is_text(self):
        placed = place(self.note("note-fenced"))
        self.assertEqual([section for section, _ in placed], ["現状"])
        quoted = placed[0][1]
        self.assertIn("## 適用条件", quoted)          # kept as part of the code block
        self.assertIn("続きの本文。", quoted)

    def test_the_sections_partition_the_body_exactly(self):
        # Every non-blank line of a body appears once across the sections: none lost to a
        # rule that stops early, none repeated by two rules claiming the same lines.
        for identifier in ("note-lead", "decision-model", LONG_ID, "note-fenced",
                           "note-trailing", "note-nested", "note-long-fence",
                           "moc-harvest", "proj-harvest"):
            with self.subTest(identifier=identifier):
                note = self.note(identifier)
                quoted = Counter(line for _, text in place(note)
                                 for line in text.splitlines() if line.strip())
                body = Counter(line for line in note["body"].strip().splitlines()
                               if line.strip())
                self.assertEqual(quoted, body)


class AccountingTests(ContextTestCase):
    """What the result says is what the document contains."""

    def test_records_match_the_document_and_dropped_does_not(self):
        for budget in (1500, 2500, 4000, 20000):
            try:
                result = self.build(budget=budget)
            except MemoryError:
                continue
            with self.subTest(budget=budget):
                for identifier in result["records"]:
                    self.assertIn(f"記録ID: {identifier} ", result["text"])
                for identifier in result["dropped"]:
                    self.assertNotIn(f"記録ID: {identifier} ", result["text"])
                self.assertEqual(sum(result["sections"].values()),
                                 result["text"].count("記録ID: "))
                self.assertEqual(set(result["records"]) | set(result["dropped"]),
                                 set(result["selected"]))

    def test_selected_and_included_are_separate(self):
        pinched = self.build(budget=self._floor())
        self.assertGreater(len(pinched["selected"]), len(pinched["records"]))

    def _floor(self):
        budget = 1
        for _ in range(8):
            try:
                self.build(budget=budget)
                return budget
            except MemoryError as exc:
                found = re.search(r"below the (\d+)", str(exc))
                if not found:
                    raise
                budget = int(found.group(1))
        self.fail("the minimum budget did not settle")

    def test_an_empty_section_says_which_kind_of_empty_it_is(self):
        pinched = self.build(budget=self._floor())
        self.assertIn("選択された記録はありますが、予算に収まらないため省きました",
                      pinched["text"])
        # With nothing selected for a section at all, the wording is the other one.
        roomy = self.build(budget=20000, depth=0)
        self.assertIn(EMPTY, roomy["text"])

    def test_no_room_for_even_a_cut_marker_yields_no_quotation(self):
        from knowledge_memory.context import _trim
        self.assertEqual(_trim("長い本文。" * 50, 5, "chars"), "")
        self.assertNotEqual(_trim("長い本文。" * 50, 400, "chars"), "")

    def test_a_record_kept_without_its_quotation_says_so(self):
        # Reached when the sources fit but nothing of the body does, not even the cut
        # marker. The branch is forced at a budget that does reach the trimming path,
        # which is more honest than hunting for one where the marker also fails to fit.
        floor = self._floor()
        result = budget = None
        with mock.patch("knowledge_memory.context._trim", return_value=""):
            for budget in range(floor, floor * 6, 25):
                result = self.build(budget=budget)
                if result["quote_omitted"]:
                    break
            else:
                self.fail("no budget reached the source-only branch")
        self.assertTrue(result["quote_omitted"])
        self.assertIn("本文を省き出典だけ残した記録", result["text"])
        self.assertIn(NO_QUOTE, result["text"])
        # The sources are still there; the entry is not a bare heading.
        for identifier in result["quote_omitted"]:
            with self.subTest(identifier=identifier):
                self.assertIn(f"記録ID: {identifier} ", result["text"])
        self.assertIn("出典:", result["text"])
        self.assertLessEqual(result["size"], budget)



class SearchFailureTests(ContextTestCase):
    """A failing search is reported; it never becomes "no hits"."""

    def test_a_goal_longer_than_the_search_allows_is_reported(self):
        with self.assertRaisesRegex(MemoryError, "the search accepts at most 512"):
            self.build(goal="x" * 513)

    def test_a_goal_that_normalises_away_skips_the_search_and_says_so(self):
        result = self.build(goal="\x00\x01")
        self.assertEqual(result["search_hits"], 0)
        self.assertIn("正規化すると空になる目的語", result["search"])
        self.assertIn("検索は実行していません", result["text"])

    def test_a_stale_index_is_not_reported_as_no_hits(self):
        extra = FIXTURE / "10-notes" / "temporary-for-test.md"
        extra.write_text(
            "---\nid: temporary-for-test\ntitle: x\nkind: note\nstatus: active\n"
            "scope: demo\nclaim_kind: observation\nupdated: '2026-09-20'\n"
            "sources:\n- ref: fixture:demo\n  locator: a\n---\n本文\n", encoding="utf-8")
        self.addCleanup(extra.unlink)
        with self.assertRaisesRegex(MemoryError, "rebuild the index"):
            self.build()


class OutputTargetTests(ContextTestCase):
    """The generated file never lands on its own inputs."""

    def test_writing_onto_the_index_is_refused(self):
        result = self.build(budget=20000)
        before = self.db.read_bytes()
        targets = {"the database itself": self.db,
                   "the same path spelled differently": self.db.parent / "." / self.db.name}
        link = self.root / "hard.sqlite3"
        link.hardlink_to(self.db)
        targets["a hard link to it"] = link
        symlink = self.root / "sym.sqlite3"
        symlink.symlink_to(self.db)
        targets["a symlink to it"] = symlink
        for label, target in targets.items():
            with self.subTest(target=label):
                with self.assertRaisesRegex(MemoryError, "overwrite the index"):
                    write_context(result, target, self.db)
        self.assertEqual(before, self.db.read_bytes())


class PartitionTests(ContextTestCase):
    """Bodies are split into sections without losing or repeating a line."""

    @staticmethod
    def note(body, kind="note", claim="observation"):
        return {"metadata": {"kind": kind, "claim_kind": claim}, "body": body}

    def assert_partitions(self, body):
        quoted = Counter(line for _, text in place(self.note(body))
                         for line in text.splitlines() if line.strip())
        original = Counter(line for line in body.strip().splitlines() if line.strip())
        self.assertEqual(quoted, original)

    def test_a_section_that_no_rule_claims_is_kept(self):
        body = "# ノート\n前置き\n## 適用条件\n条件A\n## 理由または根拠\n根拠本文\n"
        placed = dict(place(self.note(body)))
        self.assertIn("根拠本文", placed["現状"])      # after a claimed heading, still kept
        self.assertIn("前置き", placed["現状"])
        self.assertIn("条件A", placed["制約"])
        self.assertNotIn("根拠本文", placed["制約"])
        self.assert_partitions(body)

    def test_a_nested_claimed_heading_belongs_to_the_inner_section_only(self):
        body = "# n\n前置き\n## 適用条件\n条件A\n### 未解決\n課題本文\n"
        placed = dict(place(self.note(body)))
        self.assertIn("課題本文", placed["未解決"])
        self.assertNotIn("課題本文", placed["制約"])
        self.assertIn("条件A", placed["制約"])
        self.assert_partitions(body)

    def test_a_nested_heading_of_the_same_name_does_not_duplicate(self):
        body = "# n\n## 適用条件\n外側\n### 適用条件\n内側\n"
        self.assert_partitions(body)
        placed = dict(place(self.note(body)))
        self.assertEqual(placed["制約"].count("内側"), 1)

    def test_a_heading_under_an_unclaimed_heading_inherits_the_lead(self):
        body = "# n\n## 内容\n本文\n### 補足\n補足本文\n"
        placed = dict(place(self.note(body)))
        self.assertIn("補足本文", placed["現状"])
        self.assert_partitions(body)

    def test_a_longer_fence_is_not_closed_by_a_shorter_one(self):
        body = "# n\n前置き\n````\n```\n## 適用条件\nコード内\n````\n後続本文\n"
        placed = place(self.note(body))
        self.assertEqual([section for section, _ in placed], ["現状"])
        quoted = placed[0][1]
        for fragment in ("コード内", "後続本文", "## 適用条件"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, quoted)
        self.assert_partitions(body)

    def test_a_closing_fence_must_match_the_opening_one(self):
        cases = [
            "# n\n~~~~\n~~~\n## 適用条件\nコード\n~~~~\n後\n",      # tilde, longer open
            "# n\n```\nコード\n```\n## 適用条件\n条件\n",            # heading after a close
            "# n\n````python\n```\n## 適用条件\n````\n後\n",         # info string
            "# n\n```\n## 適用条件\n``` これは閉じない\nコード\n```\n後\n",
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assert_partitions(body)
        # A heading inside a fence is content; one after a genuine close is a heading.
        self.assertEqual([s for s, _ in place(self.note(cases[0]))], ["現状"])
        self.assertIn("制約", [s for s, _ in place(self.note(cases[1]))])
        self.assertEqual([s for s, _ in place(self.note(cases[2]))], ["現状"])
        self.assertEqual([s for s, _ in place(self.note(cases[3]))], ["現状"])

    def test_the_document_keeps_the_whole_body_when_the_budget_allows(self):
        # Through build_context, not just place(): the quoted lines still cover the body.
        result = self.build(budget=60000, depth=5)
        self.assertEqual(result["truncated"], [])
        self.assertEqual(result["quote_omitted"], [])
        self.assertEqual(result["dropped"], [])
        quoted = Counter(line[6:] for line in result["text"].splitlines()
                         if line.startswith("    > ") and line[6:].strip())
        from knowledge_memory.core import read_notes
        notes = {n["metadata"]["id"]: n for n in read_notes(self.db, "demo")}
        expected = Counter()
        for identifier in result["records"]:
            for line in notes[identifier]["body"].strip().splitlines():
                if line.strip():
                    expected[line] += 1
        self.assertEqual(quoted, expected)

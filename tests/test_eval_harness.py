"""The classifier, the scorer and the ledger, on invented sessions.

The case that matters most is the one that would produce wrong numbers: a session the API
refused, for example with HTTP 429, makes no tool calls and cites nothing, which looks exactly
like a correct "nothing is recorded" answer. It must be rejected before scoring, not scored.

Every session here is a few invented JSON lines. No real questions, answers or records.
"""

from __future__ import annotations

import json
import unittest

from knowledge_memory.core import MemoryError
from knowledge_memory.eval_budget import REPORT_FAILURE_MARKER
from knowledge_memory.eval_harness import (BUDGET_FIELDS, GROUNDS_EMPTY, GROUNDS_IDS,
                                           GROUNDS_MALFORMED, GROUNDS_MISSING, RunLedger,
                                           cited_records, parse_grounds, report_is_expected,
                                           retrieval_totals, score, session_outcome,
                                           undecorated, validated_report)

LIMITS = {"searches": 6, "documents": 3, "characters": 40000}
REPORT = {"limits": LIMITS,
          "spent": {"searches": 0, "documents": 2, "characters": 26592}, "refusals": []}

CORPUS = ["src-aaaa", "src-bbbb", "src-cccc"]
NOT_COVERED = {"id": "u01", "kind": "not_covered", "query": "x", "expect": "unknown"}
ONE = {"id": "q01", "kind": "single", "query": "x", "expect_ids": ["src-aaaa"]}
TWO = {"id": "q02", "kind": "multi", "query": "x",
       "expect_ids": ["src-aaaa", "src-bbbb"], "min_hits": 2}


def session(answer, *, is_error=False, api_status=None, calls=0, tool_text="", cost=0.01,
            with_result=True):
    lines = []
    for _ in range(calls):
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t", "name": "mcp__km__search_memory", "input": {}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t", "content": tool_text}]}}))
    if with_result:
        event = {"type": "result", "result": answer, "total_cost_usd": cost}
        if is_error:
            event.update(is_error=True, api_error_status=api_status)
        lines.append(json.dumps(event))
    return lines


REFUSED_429 = session("You've hit your session limit", is_error=True, api_status=429, cost=0)


class ClassifyTests(unittest.TestCase):
    def test_a_refused_session_is_not_usable(self):
        outcome = session_outcome(REFUSED_429)
        self.assertFalse(outcome["usable"])
        self.assertEqual(outcome["api_error_status"], 429)
        self.assertEqual(outcome["tool_calls"], 0)
        self.assertEqual(outcome["answer"], "")

    def test_a_session_with_no_result_event_is_not_usable(self):
        outcome = session_outcome(session("anything", with_result=False))
        self.assertFalse(outcome["usable"])
        self.assertIn("did not finish", outcome["reason"])

    def test_an_empty_answer_is_not_usable(self):
        self.assertFalse(session_outcome(session("   "))["usable"])

    def test_a_finished_session_is_usable_and_its_spend_is_read(self):
        outcome = session_outcome(session("答え\nRECORDS: src-aaaa", calls=2,
                                          tool_text="x" * 50, cost=0.25))
        self.assertTrue(outcome["usable"])
        self.assertEqual(outcome["tool_calls"], 2)
        self.assertEqual(outcome["result_chars"], 100)
        self.assertEqual(outcome["cost_usd"], 0.25)

    def test_budget_refusals_are_counted(self):
        outcome = session_outcome(session("答え\nRECORDS: none", calls=3,
                                          tool_text="retrieval budget exhausted: characters"))
        self.assertEqual(outcome["refusals"], 3)


class RefusedSessionIsNeverScoredTests(unittest.TestCase):
    """The regression this file exists for."""

    def test_scoring_a_refused_session_is_refused(self):
        outcome = session_outcome(REFUSED_429)
        with self.assertRaises(MemoryError) as caught:
            score(NOT_COVERED, [], outcome, CORPUS)
        self.assertIn("unusable session", str(caught.exception))

    def test_a_refused_session_would_otherwise_have_passed_a_not_covered_question(self):
        """Shows the false pass this guard removes, without letting it through."""
        refused = session_outcome(REFUSED_429)
        self.assertEqual(refused["tool_calls"], 0)
        self.assertEqual(cited_records(refused["answer"]), [])
        # The same empty shape from a session that did finish is a genuine pass, so the only
        # thing separating the two is the classifier.
        finished = session_outcome(session("記録は見つかりませんでした\nRECORDS: none"))
        self.assertTrue(score(NOT_COVERED, [], finished, CORPUS)["cited_ids_match_expected"])

    def test_a_refused_session_is_refused_for_every_question_shape(self):
        outcome = session_outcome(REFUSED_429)
        for question in (NOT_COVERED, ONE, TWO):
            with self.assertRaises(MemoryError):
                score(question, ["src-aaaa", "src-bbbb"], outcome, CORPUS)


class CitedRecordsTests(unittest.TestCase):
    def test_none_and_a_missing_line_both_mean_nothing_was_cited(self):
        for answer in ("答え\nRECORDS: none", "答え\nRECORDS:  None ", "答えだけ", ""):
            self.assertEqual(cited_records(answer), [])

    def test_ids_are_read_whatever_separates_or_decorates_them(self):
        self.assertEqual(cited_records("答え\nRECORDS: `src-bbbb`, src-aaaa"),
                         ["src-aaaa", "src-bbbb"])
        self.assertEqual(cited_records("答え\nRECORDS: src-aaaa src-bbbb"),
                         ["src-aaaa", "src-bbbb"])

    def test_prose_mentioning_a_record_is_not_a_citation(self):
        self.assertEqual(cited_records("src-aaaa を見た\nRECORDS: none"), [])


class ScoringTests(unittest.TestCase):
    def outcome(self, answer, **kwargs):
        return session_outcome(session(answer, **kwargs))

    def test_min_hits_needs_that_many_expected_records(self):
        partial = score(TWO, ["src-aaaa"], self.outcome("答え\nRECORDS: src-aaaa"), CORPUS)
        self.assertFalse(partial["retrieval_success"])
        self.assertFalse(partial["cited_ids_match_expected"])
        both = score(TWO, ["src-aaaa", "src-bbbb"],
                     self.outcome("答え\nRECORDS: src-aaaa, src-bbbb"), CORPUS)
        self.assertTrue(both["retrieval_success"])
        self.assertTrue(both["cited_ids_match_expected"])

    def test_a_not_covered_question_fails_and_is_a_wrong_source_when_anything_is_cited(self):
        row = score(NOT_COVERED, ["src-aaaa"], self.outcome("答え\nRECORDS: src-aaaa"), CORPUS)
        self.assertFalse(row["retrieval_success"])
        self.assertFalse(row["cited_ids_match_expected"])
        self.assertTrue(row["wrong_source"])

    def test_citing_an_id_outside_the_corpus_is_a_wrong_source(self):
        row = score(ONE, ["src-aaaa"], self.outcome("答え\nRECORDS: src-aaaa, src-zzzz"), CORPUS)
        self.assertTrue(row["wrong_source"])

    def test_citing_another_real_record_is_recorded_but_not_an_error(self):
        row = score(ONE, ["src-aaaa"], self.outcome("答え\nRECORDS: src-aaaa, src-cccc"), CORPUS)
        self.assertFalse(row["wrong_source"])
        self.assertEqual(row["cited_outside_expected"], 1)

    def test_retrieved_but_not_cited_is_reported_separately_from_retrieval(self):
        row = score(ONE, ["src-aaaa"], self.outcome("答え\nRECORDS: none"), CORPUS)
        self.assertTrue(row["retrieval_success"])
        self.assertFalse(row["cited_ids_match_expected"])
        self.assertTrue(row["retrieved_but_not_cited"])

    def test_prose_fidelity_is_never_decided_here(self):
        row = score(ONE, ["src-aaaa"], self.outcome("答え\nRECORDS: src-aaaa"), CORPUS)
        self.assertEqual(row["prose_fidelity"], "unverified_pending_human_review")

    def test_an_impossible_min_hits_is_rejected(self):
        bad = {"id": "q", "query": "x", "expect_ids": ["src-aaaa"], "min_hits": 2}
        with self.assertRaises(MemoryError):
            score(bad, ["src-aaaa"], self.outcome("答え\nRECORDS: src-aaaa"), CORPUS)


class RetrievalTotalsTests(unittest.TestCase):
    def test_a_harness_side_first_search_is_visible_in_the_total(self):
        totals = retrieval_totals(1, 1473, REPORT)
        self.assertEqual(totals["server_searches"], 0)
        self.assertEqual(totals["total_searches"], 1)
        self.assertEqual(totals["total_characters"], 1473 + 26592)
        self.assertTrue(totals["report_present"])

    def test_a_model_driven_condition_reports_no_harness_spend(self):
        report = {"limits": LIMITS,
                  "spent": {"searches": 4, "documents": 1, "characters": 22449}}
        totals = retrieval_totals(0, 0, report)
        self.assertEqual(totals["total_searches"], 4)
        self.assertEqual(totals["total_characters"], 22449)

    def test_a_missing_required_report_is_refused_not_counted_as_zero(self):
        """Absent evidence and a genuine spend of zero must not aggregate to the same number."""
        with self.assertRaises(MemoryError) as caught:
            retrieval_totals(1, 10, None)
        self.assertIn("missing", str(caught.exception))
        allowed = retrieval_totals(1, 10, None, report_required=False)
        self.assertFalse(allowed["report_present"])
        self.assertEqual(allowed["documents"], 0)

    def test_a_malformed_report_is_refused(self):
        for bad in ({}, {"spent": {}}, {"limits": {}}, {"limits": {}, "spent": []}, "text", 7):
            with self.assertRaises(MemoryError):
                retrieval_totals(0, 0, bad)

    def test_validated_report_returns_a_checked_copy(self):
        checked = validated_report(REPORT)
        self.assertEqual(checked["spent"], REPORT["spent"])
        self.assertEqual(checked["limits"], REPORT["limits"])
        self.assertIsNot(checked["spent"], REPORT["spent"])   # a copy, so callers cannot edit it
        self.assertIsNone(validated_report(None, required=False))


class LedgerTests(unittest.TestCase):
    def test_every_attempt_counts_including_the_ones_that_failed(self):
        ledger = RunLedger(3, 10.0)
        for _ in range(3):
            ledger.reserve()
            ledger.record(session_outcome(REFUSED_429))
        self.assertEqual(ledger.report()["attempts"], 3)
        self.assertEqual(ledger.report()["failed_attempts"], 3)
        self.assertEqual(ledger.report()["usable_sessions"], 0)
        with self.assertRaises(MemoryError) as caught:
            ledger.reserve()          # a retry would exceed the cap, so it is not allowed
        self.assertIn("session cap reached", str(caught.exception))

    def test_the_cost_cap_stops_the_run(self):
        ledger = RunLedger(100, 0.5)
        ledger.reserve()
        ledger.record(session_outcome(session("答え\nRECORDS: none", cost=0.6)))
        with self.assertRaises(MemoryError) as caught:
            ledger.reserve()
        self.assertIn("cost cap reached", str(caught.exception))

    def test_remaining_sessions_shrinks_with_failures_too(self):
        ledger = RunLedger(5, 10.0)
        ledger.reserve(); ledger.record(session_outcome(REFUSED_429))
        ledger.reserve(); ledger.record(session_outcome(session("答え\nRECORDS: none")))
        self.assertEqual(ledger.remaining_sessions, 3)

    def test_a_nonsense_cap_is_rejected(self):
        for args in ((0, 1.0), (-1, 1.0), (1, 0), (1, -1)):
            with self.assertRaises(MemoryError):
                RunLedger(*args)


if __name__ == "__main__":
    unittest.main()


class ReportFailureInvalidatesTheSessionTests(unittest.TestCase):
    """A session whose own spend record was lost is not evidence, however it then finished."""

    def unwritable(self, answer):
        message = (f"the {REPORT_FAILURE_MARKER}, so this session is not valid evidence; "
                   "stop and fix the report destination")
        return session_outcome(session(answer, calls=1, tool_text=message))

    def test_the_session_is_not_usable_even_though_the_model_finished(self):
        outcome = self.unwritable("レポートが書けませんでした\nRECORDS: none")
        self.assertFalse(outcome["usable"])
        self.assertTrue(outcome["report_failed"])
        self.assertIn("cannot be accounted for", outcome["reason"])

    def test_it_does_not_pass_a_not_covered_question(self):
        """The false pass this guard removes: the answer looks like a correct "nothing found"."""
        outcome = self.unwritable("記録は見つかりませんでした\nRECORDS: none")
        with self.assertRaises(MemoryError) as caught:
            score(NOT_COVERED, [], outcome, CORPUS)
        self.assertIn("unusable session", str(caught.exception))

    def test_it_is_distinguished_from_ordinary_budget_exhaustion(self):
        exhausted = session_outcome(session("予算切れです\nRECORDS: none", calls=2,
                                            tool_text="retrieval budget exhausted: characters"))
        self.assertTrue(exhausted["usable"])          # running out is a valid observation
        self.assertFalse(exhausted["report_failed"])
        self.assertEqual(exhausted["refusals"], 2)
        self.assertTrue(score(NOT_COVERED, [], exhausted, CORPUS)["cited_ids_match_expected"])


class GroundsLineTests(unittest.TestCase):
    """A declaration that cannot be read is not a declaration that nothing was cited."""

    def status(self, answer):
        return parse_grounds(answer)["status"]

    def test_the_four_shapes_are_distinguished(self):
        self.assertEqual(self.status("答えだけ"), GROUNDS_MISSING)
        self.assertEqual(self.status("答え\nRECORDS:"), GROUNDS_MALFORMED)
        self.assertEqual(self.status("答え\nRECORDS: none"), GROUNDS_EMPTY)
        self.assertEqual(self.status("答え\nRECORDS: src-aaaa"), GROUNDS_IDS)

    def test_none_is_accepted_only_on_its_own(self):
        self.assertEqual(self.status("答え\nRECORDS: None"), GROUNDS_EMPTY)
        self.assertEqual(self.status("答え\nRECORDS: none, src-aaaa"), GROUNDS_MALFORMED)
        self.assertEqual(self.status("答え\nRECORDS: src-aaaa, none"), GROUNDS_MALFORMED)

    def test_a_token_is_validated_whole_and_never_trimmed(self):
        grounds = parse_grounds("答え\nRECORDS: src-aaaa.extra")
        self.assertEqual(grounds["ids"], ["src-aaaa.extra"])   # not trimmed to src-aaaa
        row = score(ONE, ["src-aaaa"], session_outcome(session("答え\nRECORDS: src-aaaa.extra")),
                    CORPUS)
        self.assertFalse(row["cited_ids_match_expected"])
        self.assertTrue(row["wrong_source"])

    def test_a_name_that_is_not_a_record_is_a_wrong_source_not_an_empty_citation(self):
        row = score(NOT_COVERED, [], session_outcome(session("答え\nRECORDS: invented-note")),
                    CORPUS)
        self.assertFalse(row["cited_ids_match_expected"])
        self.assertTrue(row["wrong_source"])

    def test_decoration_around_an_id_is_stripped(self):
        self.assertEqual(parse_grounds("答え\nRECORDS: `src-aaaa`, **src-bbbb**")["ids"],
                         ["src-aaaa", "src-bbbb"])

    def test_a_split_identifier_becomes_two_ids_that_match_nothing(self):
        """Whitespace inside an id cannot be repaired, so it must not resolve to a real record."""
        grounds = parse_grounds("答え\nRECORDS: src aaaa")
        self.assertEqual(grounds["ids"], ["aaaa", "src"])
        row = score(ONE, ["src-aaaa"], session_outcome(session("答え\nRECORDS: src aaaa")), CORPUS)
        self.assertFalse(row["cited_ids_match_expected"])
        self.assertTrue(row["wrong_source"])

    def test_a_missing_line_does_not_pass_a_not_covered_question(self):
        row = score(NOT_COVERED, [], session_outcome(session("記録はありません")), CORPUS)
        self.assertFalse(row["cited_ids_match_expected"])
        self.assertTrue(row["grounds_unreadable"])
        self.assertEqual(row["grounds_status"], GROUNDS_MISSING)

    def test_the_last_declaration_is_the_one_read(self):
        answer = "下書き\nRECORDS: src-bbbb\n訂正します\nRECORDS: src-aaaa"
        self.assertEqual(parse_grounds(answer)["ids"], ["src-aaaa"])

    def test_the_status_is_reported_on_every_scored_row(self):
        row = score(ONE, ["src-aaaa"], session_outcome(session("答え\nRECORDS: src-aaaa")), CORPUS)
        self.assertEqual(row["grounds_status"], GROUNDS_IDS)
        self.assertFalse(row["grounds_unreadable"])


class ReportExpectationTests(unittest.TestCase):
    def test_a_session_that_made_no_call_is_not_expected_to_have_a_report(self):
        outcome = session_outcome(session("答え\nRECORDS: none", calls=0))
        self.assertEqual(outcome["tool_calls"], 0)
        self.assertFalse(report_is_expected(outcome))
        totals = retrieval_totals(1, 2, None, report_required=report_is_expected(outcome))
        self.assertFalse(totals["report_present"])
        self.assertEqual(totals["total_searches"], 1)

    def test_a_session_that_did_call_must_have_one(self):
        outcome = session_outcome(session("答え\nRECORDS: src-aaaa", calls=1, tool_text="x"))
        self.assertTrue(report_is_expected(outcome))
        with self.assertRaises(MemoryError):
            retrieval_totals(0, 0, None, report_required=report_is_expected(outcome))


class IdentifierDecorationTests(unittest.TestCase):
    """Stripping markup must not remove a character the identifier is allowed to contain."""

    def test_a_matching_pair_of_fences_is_removed(self):
        self.assertEqual(undecorated("`src-aaaa`"), "src-aaaa")
        self.assertEqual(undecorated("**src-aaaa**"), "src-aaaa")
        self.assertEqual(undecorated("  *src-aaaa*  "), "src-aaaa")

    def test_an_unmatched_fence_is_kept_so_the_token_fails_validation(self):
        self.assertEqual(undecorated("*src-aaaa"), "*src-aaaa")
        self.assertEqual(parse_grounds("答え\nRECORDS: *src-aaaa")["status"], GROUNDS_MALFORMED)

    def test_a_trailing_underscore_is_part_of_the_identifier(self):
        """src-aaaa_ is a valid identifier and a different record from src-aaaa."""
        self.assertEqual(undecorated("src-aaaa_"), "src-aaaa_")
        grounds = parse_grounds("答え\nRECORDS: src-aaaa_")
        self.assertEqual(grounds["status"], GROUNDS_IDS)
        self.assertEqual(grounds["ids"], ["src-aaaa_"])
        row = score(ONE, ["src-aaaa"], session_outcome(session("答え\nRECORDS: src-aaaa_")), CORPUS)
        self.assertFalse(row["cited_ids_match_expected"])
        self.assertTrue(row["wrong_source"])

    def test_none_with_a_trailing_underscore_is_not_a_declaration_of_no_grounds(self):
        self.assertEqual(undecorated("none_"), "none_")
        self.assertEqual(parse_grounds("答え\nRECORDS: none_")["status"], GROUNDS_IDS)
        row = score(NOT_COVERED, [], session_outcome(session("答え\nRECORDS: none_")), CORPUS)
        self.assertFalse(row["cited_ids_match_expected"])
        self.assertTrue(row["wrong_source"])

    def test_an_appended_suffix_is_still_not_trimmed(self):
        self.assertEqual(parse_grounds("答え\nRECORDS: src-aaaa.extra")["ids"], ["src-aaaa.extra"])


class EmptyDecorationTests(unittest.TestCase):
    def test_markup_only_tokens_are_not_discarded_into_a_false_pass(self):
        for declaration in ('none, **', 'none, ``', '**', '``'):
            with self.subTest(declaration=declaration):
                answer = 'RECORDS: ' + declaration
                self.assertEqual(parse_grounds(answer)['status'], GROUNDS_MALFORMED)
                row = score(NOT_COVERED, [], session_outcome(session(answer)), CORPUS)
                self.assertFalse(row['cited_ids_match_expected'])
                self.assertTrue(row['grounds_unreadable'])


class ReportCompletenessTests(unittest.TestCase):
    """A report that recorded nothing must not read as a session that spent nothing."""

    def spent(self, **overrides):
        base = {"searches": 1, "documents": 1, "characters": 100}
        base.update(overrides)
        return {"limits": LIMITS, "spent": base, "refusals": []}

    def test_a_hollow_report_is_refused(self):
        with self.assertRaises(MemoryError) as caught:
            validated_report({"limits": {}, "spent": {}})
        self.assertIn("exactly", str(caught.exception))

    def test_a_partial_report_is_not_completed_with_zeroes(self):
        for section in ("limits", "spent"):
            report = self.spent()
            report[section] = {"searches": 2}
            with self.assertRaises(MemoryError) as caught:
                validated_report(report)
            self.assertIn(section, str(caught.exception))

    def test_an_extra_field_is_refused(self):
        with self.assertRaises(MemoryError):
            validated_report(self.spent(extra=1))

    def test_a_negative_count_is_refused(self):
        with self.assertRaises(MemoryError) as caught:
            validated_report(self.spent(searches=-1))
        self.assertIn("non-negative", str(caught.exception))

    def test_a_boolean_is_refused_because_it_would_count_as_one(self):
        for field in BUDGET_FIELDS:
            with self.assertRaises(MemoryError):
                validated_report(self.spent(**{field: True}))
            with self.assertRaises(MemoryError):
                validated_report(self.spent(**{field: False}))

    def test_a_float_is_refused(self):
        with self.assertRaises(MemoryError):
            validated_report(self.spent(characters=100.5))

    def test_refusals_must_be_a_list_when_present(self):
        report = self.spent()
        report["refusals"] = "one"
        with self.assertRaises(MemoryError) as caught:
            validated_report(report)
        self.assertIn("refusals", str(caught.exception))
        del report["refusals"]
        self.assertEqual(validated_report(report)["refusals"], [])

    def test_zero_is_a_legitimate_spend_when_every_field_says_so(self):
        report = self.spent(searches=0, documents=0, characters=0)
        totals = retrieval_totals(1, 2, report)
        self.assertEqual(totals["total_searches"], 1)
        self.assertEqual(totals["total_characters"], 2)
        self.assertTrue(totals["report_present"])

    def test_harness_spend_must_also_be_non_negative_integers(self):
        for args in ((-1, 0), (0, -1), (True, 0), (1.5, 0)):
            with self.assertRaises(MemoryError):
                retrieval_totals(*args, REPORT)


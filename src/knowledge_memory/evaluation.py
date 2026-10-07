"""Run a fixed question set against an index and report hits, misses and false hits.

This measures retrieval only. A hit means the expected record was returned, not that its
claim is true, and questions marked unknown are expected to return nothing.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .core import MemoryError, search_notes

ALLOWED = {"id", "question", "query", "expect_ids", "expect", "note",
           "min_hits", "evidence", "kind"}


def load_questions(path: Path, allow_legacy_list: bool = False) -> list[dict]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MemoryError(f"{path.name}: invalid YAML") from exc
    if allow_legacy_list and isinstance(data, list):
        # The original hybrid experiment used a JSON list and empty expect_ids for unknown.
        data = {"questions": [dict(item, expect=item.get("expect",
                "ids" if item.get("expect_ids") else "unknown"))
                if isinstance(item, dict) else item for item in data]}
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise MemoryError(f"{path.name}: expected a mapping with a questions list")
    questions = data["questions"]
    if not questions:
        raise MemoryError(f"{path.name}: no questions")
    seen = set()
    for item in questions:
        if not isinstance(item, dict) or item.keys() - ALLOWED or not item.get("id"):
            raise MemoryError(f"{path.name}: each question needs an id and only {sorted(ALLOWED)}")
        if item["id"] in seen:
            raise MemoryError(f"{path.name}: duplicate question id {item['id']}")
        seen.add(item["id"])
        if not item.get("query"):
            raise MemoryError(f"{path.name}: {item['id']} has no query")
        expect = item.get("expect", "ids")
        if expect not in ("ids", "unknown"):
            raise MemoryError(f"{path.name}: {item['id']} expect must be ids or unknown")
        if expect == "ids" and not item.get("expect_ids"):
            raise MemoryError(f"{path.name}: {item['id']} needs expect_ids")
        if expect == "unknown" and item.get("expect_ids"):
            raise MemoryError(f"{path.name}: {item['id']} cannot expect ids and unknown")
        wanted = item.get("min_hits", 1)
        if not isinstance(wanted, int) or isinstance(wanted, bool) or wanted < 1:
            raise MemoryError(f"{path.name}: {item['id']} min_hits must be a positive integer")
        if expect != "unknown" and wanted > len(item.get("expect_ids") or []):
            raise MemoryError(f"{path.name}: {item['id']} min_hits exceeds expect_ids")
    return questions


def run_evaluation(database: Path, questions: Path, scope: str,
                   include_candidates: bool = False, limit: int = 10) -> dict:
    """Imported notes stay candidate, so measuring them needs include_candidates."""
    results, hit, miss, false_hit = [], 0, 0, 0
    for item in load_questions(questions):
        found = [r["id"] for r in search_notes(database, item["query"], scope, limit,
                                               include_candidates=include_candidates)]
        if item.get("expect", "ids") == "unknown":
            outcome = "unknown_as_expected" if not found else "false_hit"
            false_hit += outcome == "false_hit"
        else:
            # min_hits lets a question ask for several records, not just any one of them.
            matched = set(item["expect_ids"]) & set(found)
            outcome = "hit" if len(matched) >= item.get("min_hits", 1) else "miss"
            hit += outcome == "hit"
            miss += outcome == "miss"
        results.append({"id": item["id"], "query": item["query"], "outcome": outcome,
                        "kind": item.get("kind", "-"),
                        "expect_ids": item.get("expect_ids", []),
                        "min_hits": item.get("min_hits", 1), "found": found})
    return {"scope": scope, "include_candidates": include_candidates, "limit": limit,
            "questions": len(results), "hit": hit, "miss": miss, "false_hit": false_hit,
            "results": results}

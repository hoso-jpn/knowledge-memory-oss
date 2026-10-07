"""Build a task context for a new AI session, out of records and nothing else.

Every line of the output is either a fixed label or a quotation attributed to exactly one
record. Nothing is summarised, merged or rephrased, so two records that disagree appear
side by side with their own sources rather than being reconciled into one statement.

The generated file is not a record. It has no frontmatter, it is written outside the
vault, and `validate` would reject it if it were ever put inside one.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .core import ID, MemoryError, index_meta, normalize, read_notes, search_notes

BUDGET_UNITS = ("chars", "tokens")
MAX_DEPTH = 5
MAX_QUERY = 512
CUT = "…〔引用はここで切れています〕"
NO_QUOTE = "〔本文は予算に収まらないため省きました。出典だけを残します〕"
# Body headings the record format documents. Used to place a record, never to infer one.
SECTION_PATTERNS = {
    "制約": r"(?:適用条件|制約)",
    "失敗例": r"(?:限界・例外|限界|例外|失敗)",
    "未解決": r"(?:未解決|残る問い)",
}
SECTIONS = ("現状", "制約", "判断", "失敗例", "未解決")
EMPTY = "該当する記録がありません。推測では補っていません。"
CUT_OFF = "選択された記録はありますが、予算に収まらないため省きました（{count}件）。"


class ContextBudgetError(MemoryError):
    """The fixed context information cannot fit in the requested budget."""

    def __init__(self, budget, minimum, unit):
        self.minimum = minimum
        super().__init__(f"budget {budget} {unit} is below the {minimum} {unit} "
                         "this document needs before any record is added")


def estimate(text: str, unit: str) -> int:
    """Characters are exact. Tokens are an estimate, never a guarantee."""
    if unit == "chars":
        return len(text)
    # One token per CJK character, one per four characters of everything else. The real
    # count belongs to the model's tokenizer, so this is an estimate that errs high.
    cjk = sum(1 for ch in text if "぀" <= ch <= "鿿" or "＀" <= ch <= "￯")
    return cjk + -(-(len(text) - cjk) // 4)


FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")


def headings(body: str) -> list[tuple[int, int, str]]:
    """(line number, level, text) for real headings. Text inside a code fence is text.

    A fence closes only on the same character, at least as long as the one that opened it,
    with nothing after it. A ``` inside a ```` block is therefore content, not a close.
    """
    found, fence = [], None
    for number, line in enumerate(body.splitlines()):
        stripped = line.strip()
        marker = FENCE.match(stripped)
        if marker:
            token, rest = marker.group(1), marker.group(2).strip()
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not rest:
                fence = None
            continue
        if fence is not None:
            continue
        match = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if match:
            found.append((number, len(match.group(1)), match.group(2)))
    return found


def _matches(title: str, pattern: str) -> bool:
    return re.fullmatch(pattern, title.strip()) is not None


def _section_of(title: str) -> str | None:
    for section, pattern in SECTION_PATTERNS.items():
        if _matches(title, pattern):
            return section
    return None


def partition(body: str) -> dict[str | None, str]:
    """Split a body into sections so that every line belongs to exactly one.

    A line belongs to the innermost heading above it that a section rule claims. A heading
    claiming nothing inherits from its nearest such ancestor, and a line under no claimed
    heading at all belongs to the lead, under the key None. Nothing is dropped, because
    every line is assigned; nothing is repeated, because it is assigned only once.
    """
    lines = body.splitlines()
    marks = {number: (level, title) for number, level, title in headings(body)}
    owners: list[str | None] = []
    stack: list[tuple[int, str | None]] = []
    for number, _ in enumerate(lines):
        if number in marks:
            level, title = marks[number]
            while stack and stack[-1][0] >= level:
                stack.pop()
            inherited = stack[-1][1] if stack else None
            stack.append((level, _section_of(title) or inherited))
        owners.append(stack[-1][1] if stack else None)

    runs: dict[str | None, list[str]] = {}
    current, buffer = object(), []
    for owner, line in zip(owners, lines):
        if owner != current:
            if buffer and "\n".join(buffer).strip():
                runs.setdefault(current, []).append("\n".join(buffer).strip())
            current, buffer = owner, []
        buffer.append(line)
    if buffer and "\n".join(buffer).strip():
        runs.setdefault(current, []).append("\n".join(buffer).strip())
    return {owner: "\n\n".join(blocks) for owner, blocks in runs.items()}


def place(note: dict) -> list[tuple[str, str]]:
    """Return (section, excerpt) pairs for one record, by documented rules only.

    The pairs come from a partition of the body, so every part of it is quoted once and
    only once: nothing is dropped in silence and nothing is repeated across sections.
    """
    meta, body = note["metadata"], note["body"].strip()
    parts = partition(body)
    lead = parts.get(None, "")
    placed = []
    if lead:
        if meta["kind"] == "decision":
            placed.append(("判断", lead))
        elif meta["claim_kind"] == "hypothesis":
            placed.append(("未解決", lead))
        elif meta["kind"] == "procedure":
            placed.append(("制約", lead))
        else:
            placed.append(("現状", lead))
    for section in SECTION_PATTERNS:
        if parts.get(section):
            placed.append((section, parts[section]))
    if not placed and body:
        placed.append(("現状", body))
    return placed


def _order(note: dict) -> tuple:
    """Newest first, then by id. Independent of the order rows came back in."""
    meta = note["metadata"]
    when = meta.get("event_at") or meta.get("updated") or ""
    return (tuple(-ord(ch) for ch in when), meta["id"])


def collect(database: Path, scope: str, goal: str, project: str | None, depth: int,
            include_candidates: bool, include_history: bool) -> tuple[list[dict], dict]:
    """Choose the records. The same index and options give the same list, in the same order."""
    states = dict(include_candidates=include_candidates, include_history=include_history)
    everything = {n["id"]: n for n in read_notes(database, scope, **states)}
    # Read once more with every state, so a link that was filtered out can be told apart
    # from one that points outside this scope. Neither is followed.
    in_scope = {n["id"] for n in read_notes(database, scope, True, True)}
    trace = {"seeds": [], "skipped_state": [], "skipped_outside_scope": [],
             "search_hits": 0, "search": "ran", "depth": depth}

    seeds = []
    if project:
        note = everything.get(project)
        if note is None:
            raise MemoryError(f"project record {project!r} is not available in scope {scope!r} "
                              f"under the selected states")
        if note["metadata"]["kind"] != "project":
            raise MemoryError(f"{project!r} is kind {note['metadata']['kind']!r}, not project")
        seeds.append(project)

    # The goal drives a literal search. Its failures are reported, never folded into "0".
    # An index that is stale or unreadable propagates from search_notes untouched.
    if len(goal) > MAX_QUERY:
        raise MemoryError(f"the goal is {len(goal)} characters; the search accepts at most "
                          f"{MAX_QUERY}. Shorten the goal, or select records with --project.")
    if not normalize(goal):
        # Defined, not an error: there is nothing to match on, so no search is run.
        trace["search"] = "skipped: 正規化すると空になる目的語"
    else:
        hits = [r["id"] for r in search_notes(database, goal, scope, 50, **states)]
        trace["search_hits"] = len(hits)
        seeds.extend(sorted(hits))
    trace["seeds"] = sorted(dict.fromkeys(seeds))

    # Breadth-first over related_ids. A visited id is never revisited, so a cycle in the
    # links terminates and a record is collected at most once.
    notes_out, seen, frontier = [], set(), [(i, 0) for i in trace["seeds"]]
    while frontier:
        identifier, level = frontier.pop(0)
        if identifier in seen:
            continue
        seen.add(identifier)
        note = everything.get(identifier)
        if note is None:
            key = "skipped_state" if identifier in in_scope else "skipped_outside_scope"
            trace[key].append(identifier)
            continue
        notes_out.append(note)
        if level < depth:
            frontier.extend((linked, level + 1)
                            for linked in note["metadata"].get("related_ids", [])
                            if linked not in seen)
    notes_out.sort(key=_order)
    return notes_out, trace


def _head(note: dict) -> str:
    """The part of an entry that must survive: identity, provenance and sources."""
    meta = note["metadata"]
    marks = []
    if meta["status"] != "active":
        marks.append(f"[{'候補' if meta['status'] == 'candidate' else meta['status']}]")
    if meta["claim_kind"] == "hypothesis":
        marks.append("[仮説]")
    if meta.get("origin"):
        marks.append("[取り込み]")
    when = meta.get("event_at") or meta.get("updated")
    head = f"- {''.join(marks)}{meta['title']}\n"
    head += (f"  - 記録ID: {meta['id']} / 種別: {meta['kind']} / 主張の種類: "
             f"{meta['claim_kind']} / 状態: {meta['status']}\n")
    head += f"  - 時点: {when} / 原本: {note['path']} / SHA-256: {note['sha256']}\n"
    origin = meta.get("origin") or {}
    if origin:
        material = (origin.get("source_metadata") or {}).get("document_date") or {}
        material_date = material.get("start") or "未記録"
        if material.get("end"):
            material_date += "〜" + material["end"]
        head += (f"  - 取得時点: {origin.get('retrieved_at', '未記録')} / "
                 f"原本編集時点: {origin.get('source_edited_at', '未記録')} / "
                 f"資料日: {material_date}（内容の検証日時ではありません）\n")
    for source in meta.get("sources", []):
        head += f"  - 出典: {source['ref']}（{source['locator']}）\n"
    if meta.get("supersedes"):
        head += f"  - この記録は次を置き換えたと述べています: {', '.join(meta['supersedes'])}\n"
    return head


def _quote(text: str) -> str:
    return "".join(f"    > {line}\n" if line.strip() else "    >\n"
                   for line in text.splitlines())


def _trim(text: str, room: int, unit: str) -> str:
    """The longest prefix of a quotation that fits, marked as cut. "" if none fits."""
    marker = _quote(CUT)
    if room <= estimate(marker, unit):
        return ""
    low, high, best = 0, len(text), ""
    while low <= high:
        middle = (low + high) // 2
        candidate = _quote(text[:middle].rstrip()) + marker
        if estimate(candidate, unit) <= room:
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    return best


def _render(header: list[str], kept: list[dict], candidates: dict[str, int],
            footer: list[str]) -> str:
    """Assemble the document from structured parts. The only place the text is built."""
    lines = list(header)
    for section in SECTIONS:
        lines += [f"## {section}", ""]
        entries = [item["text"] for item in kept if item["section"] == section]
        if entries:
            lines += entries + [""]
        elif candidates.get(section):
            # Records were selected for this section; the budget is why they are absent.
            lines += [CUT_OFF.format(count=candidates[section]), ""]
        else:
            lines += [EMPTY, ""]
    return "\n".join(lines + footer) + "\n"


def _lost(kept: list[dict], placements: list[tuple]) -> tuple[list[str], list[str]]:
    """Records absent altogether, and records present but missing a section."""
    present = {item["id"] for item in kept}
    pairs = {(item["id"], item["section"]) for item in kept}
    gone = sorted({n["metadata"]["id"] for _, n, _ in placements
                   if n["metadata"]["id"] not in present})
    partial = sorted({f"{n['metadata']['id']}（{s}）" for s, n, _ in placements
                      if n["metadata"]["id"] in present and (n["metadata"]["id"], s) not in pairs})
    return gone, partial


def build_context(database: Path, scope: str, goal: str, project: str | None = None,
                  budget: int = 8000, budget_unit: str = "chars", depth: int = 2,
                  include_candidates: bool = False, include_history: bool = False,
                  generated_at: str | None = None) -> dict:
    """Render the context. The finished document is measured before it is returned."""
    if not isinstance(scope, str) or not ID.fullmatch(scope):
        raise MemoryError("an explicit scope is required")
    if budget_unit not in BUDGET_UNITS:
        raise MemoryError(f"budget unit must be one of {BUDGET_UNITS}")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise MemoryError("budget must be a positive integer")
    if not isinstance(depth, int) or isinstance(depth, bool) or not 0 <= depth <= MAX_DEPTH:
        raise MemoryError(f"depth must be between 0 and {MAX_DEPTH}")
    if not isinstance(goal, str) or not goal.strip():
        raise MemoryError("a goal is required")

    notes, trace = collect(database, scope, goal, project, depth,
                           include_candidates, include_history)
    meta = index_meta(database)
    stamp = generated_at or datetime.now(timezone.utc).isoformat()
    unit_note = ("この文書全体。出典と固定文を含む" if budget_unit == "chars"
                 else "この文書全体の概算。実際のトークン数はモデル依存で、上限は保証しません")

    header = [
        "<!-- このファイルは記録ではありません。生成物であり、vaultへ戻さないでください。 -->",
        "# タスク用文脈",
        "",
        f"- 目的: {goal}",
        f"- scope: {scope}",
        f"- project: {project or '(指定なし)'}",
        f"- 予算: {budget} {budget_unit}（{unit_note}）",
        f"- 対象の状態: {'active + candidate' if include_candidates else 'active のみ'}"
        f"{' + 過去の記録' if include_history else ''}",
        f"- 関連IDの探索深さ: {depth}",
        f"- 索引: {meta.get('vault')} / schema {meta.get('schema_version')}",
        f"- 生成時刻: {stamp}",
        "",
        "この文書は記録の引用だけでできています。要約も統合もしていないので、"
        "食い違う記録はそのまま並びます。",
    ]
    if include_candidates:
        header.append("`[候補]` は未確認の取り込み記録です。確認済みの事実ではありません。")
    header.append("`[仮説]` は主張の種類が hypothesis の記録です。")
    if trace["search"] != "ran":
        header.append(f"目的語による検索は実行していません（{trace['search']}）。")
    elif trace["search_hits"] == 0:
        header.append(
            "目的語による検索は0件でした。現在の検索は字面の一致だけなので、"
            "**根拠が存在しないことを意味しません**。")
    header += ["", "---", ""]

    placements = [(section, note, excerpt)
                  for note in notes for section, excerpt in place(note)]
    candidates = {}
    for section, _, _ in placements:
        candidates[section] = candidates.get(section, 0) + 1

    def tail(kept: list[dict]) -> list[str]:
        out = ["", "---", "", "## この文書の作り方", "",
               f"- 採用した記録: {len({i['id'] for i in kept})}件"
               f"（選択 {len(notes)}件、重複なし・循環は訪問済み集合で停止）",
               f"- 起点: {', '.join(trace['seeds']) or '(なし)'}",
               f"- 目的語の検索ヒット: {trace['search_hits']}件",
               "- 記録は「時点の新しい順、同時点はID順」で並べています。"
               "同じ索引と同じ指定なら、並びも選択も同じになります（生成時刻を除く）。",
               "- 節への振り分けは記録の kind と本文見出し（適用条件・限界・例外・未解決）"
               "だけで決めています。推測はしていません。"]
        if trace["skipped_outside_scope"]:
            out.append("- このscopeに無いため辿らなかった関連ID: "
                       + ", ".join(sorted(set(trace["skipped_outside_scope"]))))
        if trace["skipped_state"]:
            out.append("- 対象の状態から外れるため辿らなかった関連ID: "
                       + ", ".join(sorted(set(trace["skipped_state"]))))
        gone, partial = _lost(kept, placements)
        if gone:
            out.append(f"- 予算内に収まらず省いた記録: {', '.join(gone)}")
        if partial:
            out.append(f"- 予算内に収まらず省いた節: {', '.join(partial)}")
        trimmed = sorted({i["id"] for i in kept if i["truncated"]})
        if trimmed:
            out.append(f"- 引用を途中で切った記録: {', '.join(trimmed)}")
        without = sorted({i["id"] for i in kept if i["quote_omitted"]})
        if without:
            out.append(f"- 本文を省き出典だけ残した記録: {', '.join(without)}")
        return out

    def size_of(kept: list[dict]) -> int:
        return estimate(_render(header, kept, candidates, tail(kept)), budget_unit)

    floor = size_of([])
    if floor > budget:
        raise ContextBudgetError(budget, floor, budget_unit)

    # Each candidate is added only if the whole rendered document still fits. Measuring
    # the finished text rather than a reservation is what keeps long ids, section names
    # and separators in the trailing lists accounted for.
    kept: list[dict] = []
    for section, note, excerpt in placements:
        item = {"section": section, "id": note["metadata"]["id"], "text": _head(note),
                "truncated": False, "quote_omitted": False}
        kept.append(item)
        if size_of(kept) > budget:
            kept.pop()                       # its sources do not fit, so it goes entirely
            continue
        head = item["text"]
        item["text"] = head + _quote(excerpt)
        if size_of(kept) <= budget:
            continue
        # Marking it first makes the trailing note part of the measurement, so the room
        # left for the quotation already allows for the line that announces the cut.
        item["text"], item["truncated"] = head, True
        trimmed = _trim(excerpt, budget - size_of(kept), budget_unit)
        if trimmed:
            item["text"] = head + trimmed
        if not trimmed or size_of(kept) > budget:
            # The quotation cannot be shown at all; say so rather than leave a bare head.
            item["truncated"] = False
            item["quote_omitted"] = True
            item["text"] = head + _quote(NO_QUOTE)
            if size_of(kept) > budget:
                kept.pop()

    text = _render(header, kept, candidates, tail(kept))
    size = estimate(text, budget_unit)
    if size > budget:
        raise MemoryError(f"the finished document is {size} {budget_unit}, over the budget of "
                          f"{budget}. The document is not returned.")

    gone, partial = _lost(kept, placements)
    return {"text": text, "scope": scope, "goal": goal, "project": project,
            "budget": budget, "budget_unit": budget_unit,
            "budget_guarantee": "exact" if budget_unit == "chars" else "estimate",
            "size": size, "size_is_estimate": budget_unit != "chars", "within_budget": True,
            "selected": [n["metadata"]["id"] for n in notes],
            "records": sorted({item["id"] for item in kept}),
            "sections": {s: sum(1 for i in kept if i["section"] == s)
                         for s in SECTIONS if any(i["section"] == s for i in kept)},
            "dropped": gone, "dropped_sections": partial,
            "truncated": sorted({i["id"] for i in kept if i["truncated"]}),
            "quote_omitted": sorted({i["id"] for i in kept if i["quote_omitted"]}),
            "skipped_state": sorted(set(trace["skipped_state"])),
            "skipped_outside_scope": sorted(set(trace["skipped_outside_scope"])),
            "generated_at": stamp, "search": trace["search"],
            "search_hits": trace["search_hits"], "seeds": trace["seeds"]}


def _same_file(left: Path, right: Path) -> bool:
    """True for the same path, a symlink to it, or another hard link to the same inode."""
    try:
        if left.exists() and right.exists():
            return os.path.samefile(left, right)
    except OSError:
        pass
    return left.resolve() == right.resolve()


def write_context(result: dict, out: Path, database: Path):
    """Write the context outside the vault, and never onto the index it was built from."""
    if _same_file(out, database):
        raise MemoryError("the context would overwrite the index it was built from")
    vault = index_meta(database).get("vault")
    if vault and out.resolve().is_relative_to(Path(vault).resolve()):
        raise MemoryError("the context must be written outside the vault, or the next "
                          "index would try to read it as a record")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result["text"], encoding="utf-8")

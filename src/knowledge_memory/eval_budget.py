"""Evaluation-only retrieval budget in front of the read-only MCP tools.

A retrieval comparison is only meaningful if every method is held to the same limit, and a
limit written in a prompt is a request, not a limit: a model can exceed a cap that is only
stated in its instructions. This module enforces it on the path that hands tool results to
the model, so a method cannot spend more and look better for it.

Counting is deliberately blunt so there is nothing to route around:

* `search_memory` spends one search.
* `get_memory` and `build_context` both return record text, so both spend one document. A
  method cannot dodge the document limit by asking for a context document instead.
* Every Unicode character of the JSON handed back by any of the three spends characters —
  `len` of the encoded string, not bytes — and a repeated request for a record already read
  spends again. Nothing is cached into a free re-read.

When a result does not fit the remaining character budget it is **not** returned and **not**
shortened. The caller is told the budget is exhausted and how much was left, because a
silently trimmed record would be indistinguishable from a record that is genuinely short, and
an answer built on it would look grounded when it is not. A refused call still spends its
call slot, so a method cannot retry forever at no cost.

Nothing here changes the product's default behaviour. `km-mcp` is untouched; this is a
separate entry point used only by the evaluation harness.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import tempfile

from .core import MemoryError, snapshot
from .mcp_server import MemoryTools, serve

SEARCH_TOOLS = frozenset({'search_memory'})
DOCUMENT_TOOLS = frozenset({'get_memory', 'build_context'})
TOOL_NAMES = ('search_memory', 'get_memory', 'build_context')


class BudgetExhausted(MemoryError):
    """Raised instead of returning a result that would exceed the budget."""


# The harness matches on this to invalidate a session, so the wording is shared rather than
# duplicated as a substring in two files.
REPORT_FAILURE_MARKER = 'retrieval budget report could not be written'


class ReportUnwritable(MemoryError):
    """Raised when the spend report cannot be kept, so the session is not valid evidence."""


def check_report_path(path: Path, vault: Path, database: Path) -> Path:
    """Refuse a report destination that could destroy what is being measured.

    The report is written repeatedly during a session, so a destination inside the vault or on
    top of the index would replace a record or the database with JSON. That is checked here,
    before anything is served, and by identity rather than by name: a symlink or a hard link
    into the vault resolves or inodes to the same file even though the path looks unrelated.
    An existing file is only accepted if it is a report already, so a stray path cannot be
    clobbered.
    """
    if path.is_symlink():
        raise MemoryError('the budget report path must not be a symlink')
    # Containment is checked before the parent exists, so a path under the vault is refused
    # for being under the vault rather than for a missing directory.
    resolved, vault_root = path.resolve(), vault.resolve()
    if resolved == vault_root or vault_root in resolved.parents:
        raise MemoryError('the budget report must not be written inside the vault')
    if resolved == database.resolve():
        raise MemoryError('the budget report must not be written over the index')
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise MemoryError('the budget report needs an existing, real parent directory')
    try:
        here = path.stat()
    except FileNotFoundError:
        here = None
    if here is not None:
        if not path.is_file():
            raise MemoryError('the budget report path exists and is not a regular file')
        for candidate in (database, *(vault / name for name in snapshot(vault))):
            try:
                other = candidate.stat()
            except OSError:
                continue
            if (here.st_dev, here.st_ino) == (other.st_dev, other.st_ino):
                raise MemoryError('the budget report path is a link to a vault file or the index')
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise MemoryError('the budget report path holds something else; choose a new file') from None
        if not (isinstance(existing, dict) and {'limits', 'spent'} <= existing.keys()):
            raise MemoryError('the budget report path holds something else; choose a new file')
    return path


class Budget:
    """Counts what a single question was allowed to retrieve."""

    def __init__(self, searches: int, documents: int, characters: int):
        for name, value in (('searches', searches), ('documents', documents),
                            ('characters', characters)):
            if type(value) is not int or value < 0:
                raise MemoryError(f'{name} budget must be a non-negative integer')
        self.limits = {'searches': searches, 'documents': documents, 'characters': characters}
        self.spent = {'searches': 0, 'documents': 0, 'characters': 0}
        self.refusals = []

    def remaining(self, kind: str) -> int:
        return self.limits[kind] - self.spent[kind]

    def _refuse(self, tool: str, kind: str, needed: int):
        self.refusals.append({'tool': tool, 'exhausted': kind, 'needed': needed,
                              'remaining': self.remaining(kind), 'limit': self.limits[kind]})
        raise BudgetExhausted(
            f'retrieval budget exhausted: {kind} limit {self.limits[kind]}, '
            f'already spent {self.spent[kind]}, this call needs {needed}. '
            'No content was returned and nothing was shortened. '
            'Answer from what you already have, or say the budget ran out before you '
            'could ground an answer.')

    def spend_call(self, tool: str):
        """Charge the call itself. Called before the tool runs."""
        kind = 'searches' if tool in SEARCH_TOOLS else 'documents'
        if self.remaining(kind) < 1:
            self._refuse(tool, kind, 1)
        self.spent[kind] += 1

    def spend_text(self, tool: str, text: str):
        """Charge the payload. A payload that does not fit is refused, never trimmed."""
        size = len(text)
        if size > self.remaining('characters'):
            self._refuse(tool, 'characters', size)
        self.spent['characters'] += size

    def report(self) -> dict:
        return {'limits': dict(self.limits), 'spent': dict(self.spent),
                'refusals': list(self.refusals)}


class BudgetedTools:
    """Same call interface as MemoryTools, with a budget and an explicit tool allowlist."""

    def __init__(self, tools: MemoryTools, budget: Budget, allowed=TOOL_NAMES,
                 report_path: Path | None = None):
        unknown = set(allowed) - set(TOOL_NAMES)
        if unknown:
            raise MemoryError(f'unknown evaluation tool: {sorted(unknown)}')
        if not allowed:
            raise MemoryError('allow at least one tool')
        self.tools, self.budget, self.allowed = tools, budget, frozenset(allowed)
        self.report_path = report_path
        # What this condition advertises, in the module's fixed order. serve() reads this, so
        # tools/list matches the allowlist instead of promising tools the condition refuses.
        self.tool_names = tuple(name for name in TOOL_NAMES if name in self.allowed)

    def _persist(self):
        # Written after every call, not at exit: a client is free to terminate the server, and
        # a spend report that only survives a clean shutdown is no evidence at all. Replaced
        # atomically so a terminated write cannot leave a truncated report that would read as
        # a smaller spend.
        if self.report_path is None:
            return
        payload = json.dumps(self.budget.report(), ensure_ascii=False)
        try:
            handle, staged = tempfile.mkstemp(dir=self.report_path.parent, suffix='.tmp')
            try:
                with os.fdopen(handle, 'w', encoding='utf-8') as writer:
                    writer.write(payload)
                os.replace(staged, self.report_path)
            except BaseException:
                Path(staged).unlink(missing_ok=True)
                raise
        except OSError as exc:
            # The report was asked for as evidence. Losing it silently would leave a session
            # that looks like a normal retrieval but cannot be accounted for, so the call fails.
            raise ReportUnwritable(
                f'the {REPORT_FAILURE_MARKER}, so this session is not valid evidence; '
                'stop and fix the report destination') from exc

    def call(self, name: str, arguments: dict):
        # An unexposed tool is refused before it can spend anything.
        if name not in self.allowed:
            raise MemoryError(f'{name} is not available in this evaluation condition')
        try:
            self.budget.spend_call(name)
            result = self.tools.call(name, arguments)
            text = json.dumps(result, ensure_ascii=False)
            self.budget.spend_text(name, text)  # raises before the caller sees the content
            return result
        finally:
            self._persist()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--scope', required=True)
    parser.add_argument('--include-candidates', action='store_true')
    parser.add_argument('--tools', default=','.join(TOOL_NAMES),
                        help='comma-separated subset of ' + ','.join(TOOL_NAMES))
    parser.add_argument('--max-searches', type=int, default=6)
    parser.add_argument('--max-documents', type=int, default=3)
    parser.add_argument('--max-characters', type=int, default=40000)
    parser.add_argument('--budget-report', type=Path,
                        help='write the spend report here when the session ends')
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        budget = Budget(args.max_searches, args.max_documents, args.max_characters)
        tools = MemoryTools(args.vault, args.db, args.scope, args.include_candidates)
        report = args.budget_report
        if report is not None:
            # Validated before anything is served, and nothing is written on a failed start:
            # an earlier version wrote the report from a finally block even when startup had
            # already failed, which replaced whatever the path pointed at.
            report = check_report_path(report, args.vault, args.db)
        adapter = BudgetedTools(
            tools, budget, [name.strip() for name in args.tools.split(',') if name.strip()],
            report)
        adapter._persist()                       # prove the destination works before serving
        import asyncio
        asyncio.run(serve(adapter))
    except (MemoryError, OSError, ImportError) as exc:
        print(f'km-eval-budget: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

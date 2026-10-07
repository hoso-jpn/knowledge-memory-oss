"""Evaluation-only: decide whether a session counts, then score it by fixed rules.

This is the part of the comparison that decides what becomes a number, so it is kept here
rather than in a throwaway script. Four rules are enforced:

* A session that never reached the model produces no tool calls and cites nothing. On a
  question whose expected answer is that nothing is recorded, that scores as a success, for
  example when the API refuses requests with HTTP 429. A session is therefore classified
  before it is scored, and an unusable one cannot be scored at all.
* Retries are sessions too. A cap that only counts the sessions that happened to succeed is
  not a cap, so the ledger counts every attempt.
* In conditions where the harness performs the first retrieval itself, the server's counter
  never sees it. Totals are reported as harness plus server so the two are not confused, and a
  report that is required but absent is refused rather than read as a spend of zero.
* A grounds line that is absent or malformed is not the same as a session that correctly found
  nothing. Turning one into the other would pass a not-covered question on a session that had
  in fact failed to write its own evidence, so the three are distinguished.

Nothing here reads a vault or talks to a model; it consumes what a run produced.
"""

from __future__ import annotations

import json
import re

from .core import ID, MemoryError
from .eval_budget import REPORT_FAILURE_MARKER

RECORDS = re.compile(r'^[ \t]*RECORDS:[ \t]*(?P<body>.*)$', re.MULTILINE)
# Only these are stripped, and only as a matching pair around the whole token. Underscore is
# deliberately absent: it is legal inside an identifier, so removing a trailing one turned
# `src-aaaa_` into a different, real record and `none_` into a declaration of no grounds.
PAIRED = '`*'
# What a grounds line can be. Only EMPTY is a session that answered "nothing is recorded";
# MISSING and MALFORMED mean the declaration could not be read, which is not the same thing.
GROUNDS_IDS, GROUNDS_EMPTY, GROUNDS_MISSING, GROUNDS_MALFORMED = (
    'ids', 'empty', 'missing', 'malformed')
# Every figure a spend report must carry. A report missing any of them is not evidence.
BUDGET_FIELDS = ('searches', 'documents', 'characters')


def undecorated(token: str) -> str:
    """Remove markup that wraps a token, and nothing that could belong to the identifier.

    A pair is only removed when the same character closes it, so `src-x` keeps a trailing
    underscore while ``src-x`` and **src-x** lose their fences. Whitespace cannot appear in an
    identifier, so it is always safe to drop.
    """
    token = token.strip()
    while len(token) >= 2 and token[0] == token[-1] and token[0] in PAIRED:
        token = token[1:-1].strip()
    return token


def parse_grounds(answer) -> dict:
    """Classify the declared grounds without turning a failure to read them into an empty set.

    Tokens are validated whole. An earlier version searched the line for anything shaped like
    an id, so `src-aaaa.extra` was silently trimmed to `src-aaaa` and counted as a correct
    citation, and a line naming something that is not a record at all counted as citing
    nothing, which passed a question whose expected answer was that nothing is recorded.
    """
    if not isinstance(answer, str):
        return {'status': GROUNDS_MISSING, 'ids': [], 'raw': ''}
    found = RECORDS.findall(answer)
    if not found:
        return {'status': GROUNDS_MISSING, 'ids': [], 'raw': ''}
    body = found[-1].strip()                     # the last declaration is the answer's own
    if not body:
        return {'status': GROUNDS_MALFORMED, 'ids': [], 'raw': body}
    raw_tokens = [token for token in re.split(r'[,\s]+', body) if token]
    tokens = [undecorated(token) for token in raw_tokens]
    # A nonempty declaration must not disappear just because it contains only markup.
    if not tokens or any(not token for token in tokens):
        return {'status': GROUNDS_MALFORMED, 'ids': [], 'raw': body}
    lowered = [token.lower() for token in tokens]
    if lowered == ['none']:
        return {'status': GROUNDS_EMPTY, 'ids': [], 'raw': body}
    if 'none' in lowered:                        # 'none' alongside ids says two things at once
        return {'status': GROUNDS_MALFORMED, 'ids': [], 'raw': body}
    if not all(ID.fullmatch(token) for token in tokens):
        return {'status': GROUNDS_MALFORMED, 'ids': [], 'raw': body}
    return {'status': GROUNDS_IDS, 'ids': sorted(set(tokens)), 'raw': body}


def cited_records(answer) -> list[str]:
    """The declared ids, or an empty list when none were declared or the line was unreadable.

    Callers that must tell those apart use parse_grounds instead.
    """
    return parse_grounds(answer)['ids']


def session_outcome(lines) -> dict:
    """Classify one recorded session. Only a finished, non-error session is usable."""
    result, calls, chars, refusals, report_failed = None, 0, 0, 0, False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get('type')
        if kind == 'assistant':
            for block in event.get('message', {}).get('content', []):
                if block.get('type') == 'tool_use':
                    calls += 1
        elif kind == 'user':
            for block in event.get('message', {}).get('content', []):
                if block.get('type') == 'tool_result':
                    payload = block.get('content')
                    text = payload if isinstance(payload, str) else ''.join(
                        part.get('text', '') for part in payload or [])
                    chars += len(text)
                    if 'retrieval budget exhausted' in text:
                        refusals += 1
                    if REPORT_FAILURE_MARKER in text:
                        # Distinct from ordinary budget exhaustion: the spend for this session
                        # cannot be accounted for, so the session is not evidence even if the
                        # model went on to finish normally.
                        report_failed = True
        elif kind == 'result':
            result = event
    if result is None:
        return {'usable': False, 'reason': 'no result event; the session did not finish',
                'api_error_status': None, 'answer': '', 'tool_calls': calls,
                'result_chars': chars, 'refusals': refusals, 'cost_usd': 0.0,
                'report_failed': report_failed}
    status = result.get('api_error_status')
    answer = result.get('result') or ''
    if result.get('is_error') or not str(answer).strip():
        return {'usable': False,
                'reason': f'session failed (api_error_status={status})',
                'api_error_status': status, 'answer': '', 'tool_calls': calls,
                'result_chars': chars, 'refusals': refusals,
                'cost_usd': float(result.get('total_cost_usd') or 0),
                'report_failed': report_failed}
    if report_failed:
        return {'usable': False,
                'reason': 'the budget report could not be written, so the spend for this '
                          'session cannot be accounted for',
                'api_error_status': None, 'answer': '', 'tool_calls': calls,
                'result_chars': chars, 'refusals': refusals,
                'cost_usd': float(result.get('total_cost_usd') or 0), 'report_failed': True}
    return {'usable': True, 'reason': '', 'api_error_status': None, 'answer': answer,
            'tool_calls': calls, 'result_chars': chars, 'refusals': refusals,
            'cost_usd': float(result.get('total_cost_usd') or 0), 'report_failed': False}


def report_is_expected(outcome: dict) -> bool:
    """Whether this session should have produced a spend report.

    The report is written when a call is served, so a session that made no calls at all leaves
    none, and its server-side spend really is zero — which the log itself shows. A session that
    did call and still has no report is missing evidence and must be excluded instead. Newer
    runs write the report once before serving, so this only matters for logs recorded before
    that; the distinction is kept because reading the two the same way is what let a lost
    report pass as a spend of zero.
    """
    return bool(outcome.get('tool_calls'))


def _counts(section, where: str) -> dict:
    """Every budget figure must be present and be a plain non-negative integer."""
    if not isinstance(section, dict) or set(section) != set(BUDGET_FIELDS):
        raise MemoryError(f'the budget report {where} must hold exactly '
                          f'{sorted(BUDGET_FIELDS)}; exclude the session')
    for field in BUDGET_FIELDS:
        value = section[field]
        # bool is an int in Python, and True would silently count as a spend of one.
        if type(value) is not int or value < 0:
            raise MemoryError(f'the budget report {where}.{field} must be a non-negative '
                              'integer; exclude the session')
    return dict(section)


def validated_report(server_report, required: bool = True) -> dict | None:
    """A report that is required must be present and complete, or it is not evidence.

    Completeness is checked field by field rather than by shape. An earlier version accepted
    `{'limits': {}, 'spent': {}}` and filled the missing figures with zero, so a report that
    recorded nothing was indistinguishable from a session that spent nothing.
    """
    if server_report is None:
        if required:
            raise MemoryError('the budget report for this session is missing, so its spend '
                              'cannot be counted; exclude the session')
        return None
    if not isinstance(server_report, dict):
        raise MemoryError('the budget report for this session is malformed; exclude the session')
    refusals = server_report.get('refusals', [])
    if not isinstance(refusals, list):
        raise MemoryError('the budget report refusals must be a list; exclude the session')
    return {'limits': _counts(server_report.get('limits'), 'limits'),
            'spent': _counts(server_report.get('spent'), 'spent'),
            'refusals': refusals}


def score(question: dict, retrieved, outcome: dict, corpus_ids) -> dict:
    """Apply the rules fixed before the run. An unusable session is never scored."""
    if not outcome.get('usable'):
        raise MemoryError('refusing to score an unusable session: ' + outcome.get('reason', ''))
    expected = set(question.get('expect_ids') or [])
    unknown = question.get('expect', 'ids' if expected else 'unknown') == 'unknown'
    minimum = question.get('min_hits', 1)
    if type(minimum) is not int or minimum < 1 or (not unknown and minimum > len(expected)):
        raise MemoryError('invalid min_hits for scoring')
    corpus = set(corpus_ids)
    grounds = parse_grounds(outcome['answer'])
    found, cited = set(retrieved), set(grounds['ids'])
    readable = grounds['status'] in (GROUNDS_IDS, GROUNDS_EMPTY)
    if unknown:
        # Only a session that declared "none" answered this question correctly. A missing or
        # unreadable declaration cannot be read as having cited nothing.
        matched = grounds['status'] == GROUNDS_EMPTY
    else:
        matched = grounds['status'] == GROUNDS_IDS and len(expected & cited) >= minimum
    return {
        'id': question['id'], 'kind': question.get('kind'),
        'unknown': unknown, 'min_hits': minimum, 'expected': sorted(expected),
        'retrieved_ids': sorted(found), 'cited_ids': sorted(cited),
        'grounds_status': grounds['status'],
        'grounds_unreadable': not readable,
        'retrieval_success': (not found) if unknown else len(expected & found) >= minimum,
        # Mechanical agreement with the expected grounds. Whether the prose follows the record
        # it cites is not decidable here and is reported as awaiting human review.
        'cited_ids_match_expected': matched,
        'prose_fidelity': 'unverified_pending_human_review',
        # Naming something that is not a record of this corpus counts, and so does an
        # unreadable declaration, because it names something that could not be resolved.
        'wrong_source': (bool(cited) or not readable) if unknown
                        else bool(cited - corpus) or grounds['status'] == GROUNDS_MALFORMED,
        'cited_outside_expected': 0 if unknown else len(cited - expected),
        'retrieved_but_not_cited': (not unknown) and len(expected & found) >= minimum
                                  and not matched,
        'refusals': outcome['refusals'], 'cost_usd': outcome['cost_usd'],
    }


def retrieval_totals(harness_searches: int, harness_characters: int, server_report,
                     report_required: bool = True) -> dict:
    """Separate what the harness spent from what the budget server counted.

    A missing report is refused rather than counted as a spend of zero: absent evidence and a
    condition that really retrieved nothing would otherwise aggregate to the same number.
    """
    if type(harness_searches) is not int or harness_searches < 0 \
            or type(harness_characters) is not int or harness_characters < 0:
        raise MemoryError('harness spend must be non-negative integers')
    server_report = validated_report(server_report, report_required)
    # After validation every field is present, so nothing is defaulted into existence here.
    spent = server_report['spent'] if server_report else dict.fromkeys(BUDGET_FIELDS, 0)
    return {
        'harness_searches': harness_searches,
        'server_searches': spent['searches'],
        'total_searches': harness_searches + spent['searches'],
        'harness_characters': harness_characters,
        'server_characters': spent['characters'],
        'total_characters': harness_characters + spent['characters'],
        'documents': spent['documents'],
        'report_present': server_report is not None,
        'note': 'a condition where the harness retrieves first shows server_searches 0; read '
                'total_searches',
    }


class RunLedger:
    """Counts every session attempt, including retries, against fixed caps."""

    def __init__(self, max_sessions: int, max_cost_usd: float):
        if type(max_sessions) is not int or max_sessions < 1:
            raise MemoryError('max_sessions must be a positive integer')
        if not isinstance(max_cost_usd, (int, float)) or max_cost_usd <= 0:
            raise MemoryError('max_cost_usd must be positive')
        self.max_sessions, self.max_cost_usd = max_sessions, float(max_cost_usd)
        self.attempts = self.failures = 0
        self.cost_usd = 0.0

    @property
    def remaining_sessions(self) -> int:
        return self.max_sessions - self.attempts

    def reserve(self):
        """Called before every attempt, successful or not."""
        if self.attempts >= self.max_sessions:
            raise MemoryError(f'session cap reached: {self.max_sessions} attempts, '
                              f'{self.failures} of them failed')
        if self.cost_usd >= self.max_cost_usd:
            raise MemoryError(f'cost cap reached: {self.cost_usd:.2f} of {self.max_cost_usd:.2f}')
        self.attempts += 1

    def record(self, outcome: dict):
        self.cost_usd += float(outcome.get('cost_usd') or 0)
        if not outcome.get('usable'):
            self.failures += 1

    def report(self) -> dict:
        return {'attempts': self.attempts, 'failed_attempts': self.failures,
                'usable_sessions': self.attempts - self.failures,
                'cost_usd': round(self.cost_usd, 4),
                'max_sessions': self.max_sessions, 'max_cost_usd': self.max_cost_usd}

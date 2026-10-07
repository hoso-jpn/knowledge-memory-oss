"""Evaluate a named local model without writing proposals to a vault or review DB."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

from .core import ID, MAX_NOTE_BYTES, MemoryError
from .inbox import (FAILED_RESPONSE, FAILED_TRUNCATED, FINISH_ABSENT, LocalModelAdapter,
                    Truncated, encode, validate)


def classification(proposal):
    return tuple(proposal.get(key) for key in ('operation', 'kind', 'attribution', 'target'))


def load_cases(path):
    raw = path.read_bytes()
    if not 1 <= len(raw) <= MAX_NOTE_BYTES:
        raise MemoryError('case file must contain 1 to 2000000 bytes')
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {'cases'}:
        raise MemoryError('case file requires cases')
    cases = data['cases']
    if not isinstance(cases, list) or not 1 <= len(cases) <= 50:
        raise MemoryError('provide 1 to 50 cases')
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'id', 'inbox', 'targets', 'expected'}:
            raise MemoryError('invalid case fields')
        cid, body, targets, expected = (case[k] for k in ('id', 'inbox', 'targets', 'expected'))
        if not isinstance(cid, str) or not ID.fullmatch(cid) or cid in seen:
            raise MemoryError('case IDs must be valid and unique')
        seen.add(cid)
        if not isinstance(body, str) or not body.strip() or not isinstance(targets, dict):
            raise MemoryError('case requires inbox text and target mapping')
        for key, target in targets.items():
            if (not ID.fullmatch(key) or not isinstance(target, dict)
                    or set(target) != {'title', 'body'}
                    or any(not isinstance(v, str) or not v.strip() for v in target.values())):
                raise MemoryError('invalid case target')
        if len(encode(targets)) > 200000:
            raise MemoryError('case target context is too large')
        if not isinstance(expected, list) or not 1 <= len(expected) <= 50:
            raise MemoryError('provide 1 to 50 expected classifications')
        for item in expected:
            if (not isinstance(item, dict) or not {'operation', 'kind', 'attribution'} <= item.keys()
                    or set(item) - {'operation', 'kind', 'attribution', 'target'}
                    or any(not isinstance(value, str) for value in item.values())):
                raise MemoryError('invalid expected classification')
            validate(dict(item, title='Expected', body='Expected', quote=body), body, targets)
    return cases, hashlib.sha256(raw).hexdigest()


BUDGET_WITHIN, BUDGET_OVER, BUDGET_UNOBSERVABLE = 'within', 'over', 'not_observable'


def _budget(requested, completion_tokens):
    """Compare what was asked for with what the server said it produced.

    This is an observation, not enforcement: the figure is the server's own claim, and a server
    that reports nothing leaves the question open rather than answered in its favour.
    """
    if type(completion_tokens) is not int or completion_tokens < 0:
        state = BUDGET_UNOBSERVABLE
    elif requested is None:
        state = BUDGET_UNOBSERVABLE
    else:
        state = BUDGET_WITHIN if completion_tokens <= requested else BUDGET_OVER
    return {'max_tokens_requested': requested, 'completion_tokens_reported': completion_tokens,
            'observation': state,
            'source': 'reported by the server; not an enforced limit'}


def evaluate(path, adapter, model_revision, *, stop_on_error=False, case_id=None):
    if not isinstance(model_revision, str) or not model_revision.strip():
        raise MemoryError('record the model weights revision or digest')
    if type(stop_on_error) is not bool:
        raise MemoryError('stop_on_error must be a boolean')
    cases, cases_sha256 = load_cases(path)  # Validate the complete suite before any request.
    if case_id is not None and (not isinstance(case_id, str)
                                or case_id not in {case['id'] for case in cases}):
        raise MemoryError('case_id must identify a case in the original suite')
    selected = cases if case_id is None else [case for case in cases if case['id'] == case_id]
    results = []
    stopped_reason = None
    for case in selected:
        started = time.perf_counter()
        row = {'id': case['id'], 'schema_valid': False, 'classification_match': False}
        try:
            proposals = adapter.propose(case['inbox'], case['targets'])
            if not isinstance(proposals, list) or not 1 <= len(proposals) <= 50:
                raise MemoryError('adapter must return 1 to 50 proposals')
            for proposal in proposals:
                validate(proposal, case['inbox'], case['targets'])
            row.update(schema_valid=True, proposals=proposals,
                       classification_match=Counter(map(classification, proposals)) ==
                       Counter(map(classification, case['expected'])))
        except Truncated:
            row['error'] = 'generation stopped at a length limit before a complete response'
            row['failure_kind'] = FAILED_TRUNCATED
        except (MemoryError, ValueError, TypeError):
            row['error'] = 'model response failed transport, schema or exact-evidence validation'
            row['failure_kind'] = FAILED_RESPONSE
        # Whatever the outcome, keep what the response said about its own generation. Without it
        # a report cannot show whether the server acted on the requested token limit, and a
        # truncated run reads the same as a malformed one.
        observed = getattr(adapter, 'last_observation', None)
        if isinstance(observed, dict):
            row['finish_reason'] = observed.get('finish_reason')
            row['finish_reason_state'] = observed.get('finish_reason_state', FINISH_ABSENT)
            row['usage'] = observed.get('usage')
        # Reported apart from the classification checks on purpose. Whether the generation stayed
        # inside the requested limit is the server's own account of itself, and a case can match
        # its expected classification while reporting more tokens than were asked for or reporting
        # nothing at all. An operator deciding whether to send the next request reads this, not
        # selection_checks_passed.
        row['generation_budget'] = _budget(getattr(adapter, 'max_tokens', None),
                                          (row.get('usage') or {}).get('completion_tokens'))
        row['seconds'] = time.perf_counter() - started
        results.append(row)
        if stop_on_error and 'error' in row:
            stopped_reason = 'case_error'
            break
    attempted_ids = {row['id'] for row in results}
    all_cases_attempted = len(results) == len(cases)
    selection_passed = len(results) == len(selected) and all(row['classification_match'] for row in results)
    return {'created_at': datetime.now(timezone.utc).isoformat(),
            'adapter': adapter.identity, 'model_revision': model_revision,
            'cases_sha256': cases_sha256, 'cases': results,
            'selected_case_ids': [case['id'] for case in selected],
            'not_run_case_ids': [case['id'] for case in cases if case['id'] not in attempted_ids],
            'all_cases_attempted': all_cases_attempted,
            'stop_on_error': stop_on_error, 'stopped_reason': stopped_reason,
            'generation_max_tokens_requested': getattr(adapter, 'max_tokens', None),
            'failure_kinds': sorted({row['failure_kind'] for row in results
                                     if 'failure_kind' in row}),
            'finish_reasons': sorted({row['finish_reason'] for row in results
                                      if row.get('finish_reason')}),
            'finish_reason_states': sorted({row['finish_reason_state'] for row in results
                                            if 'finish_reason_state' in row}),
            'generation_budget_observations': sorted({row['generation_budget']['observation']
                                                      for row in results
                                                      if 'generation_budget' in row}),
            'selection_checks_passed': selection_passed,
            'automated_checks_passed': all_cases_attempted and selection_passed,
            'human_semantic_review': 'required', 'vault_modified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--model-revision', required=True)
    parser.add_argument('--case-id', help='evaluate one case from the unchanged suite')
    parser.add_argument('--stop-on-error', action='store_true',
                        help='stop after a transport, schema or exact-evidence error')
    parser.add_argument('--max-tokens', type=int, help='request a positive generation token limit')
    args = parser.parse_args(argv)
    try:
        if not args.model.strip():
            raise MemoryError('model name is required')
        adapter = LocalModelAdapter(args.endpoint, args.model, max_tokens=args.max_tokens)
        result = evaluate(args.cases, adapter, args.model_revision,
                          stop_on_error=args.stop_on_error, case_id=args.case_id)
        print(encode(result))
        return 0 if result['selection_checks_passed'] else 2
    except (MemoryError, OSError, ValueError):
        print('{"error":"model evaluation refused; inspect cases and connection"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

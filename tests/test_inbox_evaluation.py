import copy
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from knowledge_memory.core import MemoryError
from knowledge_memory.inbox import (FAILED_RESPONSE, FAILED_TRUNCATED, FINISH_ABSENT,
                                    FINISH_INVALID, FINISH_KNOWN, FINISH_REASONS,
                                    FINISH_UNKNOWN, LocalModelAdapter)
from knowledge_memory.inbox_evaluation import BUDGET_OVER, BUDGET_UNOBSERVABLE, BUDGET_WITHIN
from knowledge_memory.inbox_evaluation import evaluate, load_cases, main


class InboxEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'cases.json'
        fixture = Path(__file__).resolve().parents[1] / 'evaluation/inbox-model-cases.json'
        self.path.write_bytes(fixture.read_bytes())
        self.cases, _ = load_cases(self.path)
        self.requests = []
        self.mode = 'valid'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(request)
                if owner.mode == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', '/never-follow')
                    self.end_headers()
                    return
                context = json.loads(request['messages'][1]['content'])
                case = next(c for c in owner.cases if c['inbox'] == context['inbox'])
                proposals = [dict(c, title='Fixture', body='Fixture summary', quote=case['inbox'])
                             for c in case['expected']]
                if (owner.mode == 'invented'
                        or owner.mode == 'second-invalid' and case['id'] == owner.cases[1]['id']):
                    proposals[0]['quote'] = 'not in the source'
                if owner.mode == 'duplicate':
                    proposals.append(copy.deepcopy(proposals[0]))
                if owner.mode == 'unhashable':
                    proposals[0]['kind'] = []
                content = json.dumps({'proposals': proposals}, ensure_ascii=False)
                choice = {'message': {'content': content}}
                if owner.mode in ('truncated', 'truncated-no-limit'):
                    choice['finish_reason'] = 'length'
                elif owner.mode != 'no-finish-reason':
                    choice['finish_reason'] = 'stop'
                body = {'choices': [choice]}
                if owner.mode == 'odd-usage':
                    body['usage'] = {'prompt_tokens': 12, 'completion_tokens': -3,
                                     'total_tokens': 'many', 'secret_note': case['inbox']}
                elif owner.mode == 'long-finish-reason':
                    choice['finish_reason'] = case['inbox'] * 40
                elif owner.mode == 'short-unknown-finish-reason':
                    choice['finish_reason'] = case['inbox'][:12]
                elif owner.mode == 'invalid-finish-reason':
                    choice['finish_reason'] = 123
                elif owner.mode == 'null-finish-reason':
                    choice['finish_reason'] = None
                elif owner.mode == 'bad-usage-types':
                    body['usage'] = {'completion_tokens': True, 'prompt_tokens': -1,
                                     'total_tokens': 1.5, 'reasoning_tokens': 7}
                elif owner.mode == 'over-budget':
                    body['usage'] = {'completion_tokens': 513}
                elif owner.mode == 'no-completion-tokens':
                    body['usage'] = {'prompt_tokens': 12}
                elif owner.mode != 'no-usage':
                    body['usage'] = {'prompt_tokens': 12, 'completion_tokens': 34,
                                     'total_tokens': 46, 'ignored_field': 'x'}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.adapter = LocalModelAdapter(f'http://127.0.0.1:{self.server.server_port}/v1', 'fixture-model')

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_http_protocol_covers_operations_attributions_without_writes(self):
        before = self.path.read_bytes()
        result = evaluate(self.path, self.adapter, 'fixture-revision')
        self.assertTrue(result['automated_checks_passed'])
        self.assertEqual(result['human_semantic_review'], 'required')
        self.assertEqual(result['model_revision'], 'fixture-revision')
        self.assertEqual(len(result['cases']), 5)
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(self.requests[-1]['model'], 'fixture-model')
        self.assertEqual(self.requests[-1]['messages'][0]['role'], 'system')
        self.assertIn('削除せよ', self.requests[-1]['messages'][1]['content'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invented_evidence_and_malformed_output_fail(self):
        for mode in ('invented', 'unhashable', 'redirect'):
            with self.subTest(mode=mode):
                self.mode = mode
                result = evaluate(self.path, self.adapter, 'fixture-revision')
                self.assertFalse(result['automated_checks_passed'])
                self.assertTrue(all(not c['schema_valid'] for c in result['cases']))

    def test_duplicate_classifications_do_not_count_as_success(self):
        self.mode = 'duplicate'
        result = evaluate(self.path, self.adapter, 'fixture-revision')
        self.assertFalse(result['automated_checks_passed'])
        self.assertTrue(all(c['schema_valid'] for c in result['cases']))

    def test_invalid_suite_is_rejected_before_requesting_model(self):
        invalid = copy.deepcopy(self.cases)
        invalid[-1]['expected'][0]['attribution'] = []
        self.path.write_text(json.dumps({'cases': invalid}))
        with self.assertRaises(MemoryError):
            evaluate(self.path, self.adapter, 'fixture-revision')
        self.assertEqual(self.requests, [])

    def test_revision_and_unique_ids_are_required(self):
        with self.assertRaises(MemoryError):
            evaluate(self.path, self.adapter, ' ')
        self.path.write_text(json.dumps({'cases': [self.cases[0], self.cases[0]]}))
        with self.assertRaises(MemoryError):
            evaluate(self.path, self.adapter, 'fixture-revision')
        self.assertEqual(self.requests, [])

    def test_stop_on_error_does_not_send_remaining_requests(self):
        for mode in ('invented', 'unhashable', 'redirect'):
            with self.subTest(mode=mode):
                self.requests.clear()
                self.mode = mode
                result = evaluate(self.path, self.adapter, 'fixture', stop_on_error=True)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(len(result['cases']), 1)
                self.assertEqual(result['not_run_case_ids'], [c['id'] for c in self.cases[1:]])
                self.assertEqual(result['stopped_reason'], 'case_error')
                self.assertFalse(result['all_cases_attempted'])
                self.assertFalse(result['automated_checks_passed'])
                self.assertFalse(result['selection_checks_passed'])

    def test_later_error_preserves_previous_valid_result(self):
        self.mode = 'second-invalid'
        result = evaluate(self.path, self.adapter, 'fixture', stop_on_error=True)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(result['cases'][0]['classification_match'])
        self.assertIn('error', result['cases'][1])
        self.assertEqual(result['not_run_case_ids'], [c['id'] for c in self.cases[2:]])
        self.assertFalse(result['automated_checks_passed'])

    def test_classification_mismatch_is_a_measurement_and_does_not_stop(self):
        self.mode = 'duplicate'
        result = evaluate(self.path, self.adapter, 'fixture', stop_on_error=True)
        self.assertEqual(len(self.requests), 5)
        self.assertTrue(result['all_cases_attempted'])
        self.assertEqual(result['not_run_case_ids'], [])
        self.assertIsNone(result['stopped_reason'])
        self.assertFalse(result['automated_checks_passed'])
        self.assertTrue(all(c['schema_valid'] for c in result['cases']))

    def test_selected_case_retains_full_suite_hash_without_claiming_suite_pass(self):
        before = self.path.read_bytes()
        _, expected_hash = load_cases(self.path)
        selected = self.cases[2]['id']
        result = evaluate(self.path, self.adapter, 'fixture', case_id=selected)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result['cases_sha256'], expected_hash)
        self.assertEqual(result['selected_case_ids'], [selected])
        self.assertEqual([c['id'] for c in result['cases']], [selected])
        self.assertEqual(result['not_run_case_ids'], [c['id'] for c in self.cases if c['id'] != selected])
        self.assertTrue(result['selection_checks_passed'])
        self.assertFalse(result['all_cases_attempted'])
        self.assertFalse(result['automated_checks_passed'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_selection_or_stop_option_is_rejected_without_http(self):
        for selection in ('absent', '', ['user-new'], 1):
            with self.subTest(selection=selection), self.assertRaises(MemoryError):
                evaluate(self.path, self.adapter, 'fixture', case_id=selection)
        for stop in (1, 'yes', None):
            with self.subTest(stop=stop), self.assertRaises(MemoryError):
                evaluate(self.path, self.adapter, 'fixture', stop_on_error=stop)
        self.assertEqual(self.requests, [])

    def test_single_case_selection_still_validates_unselected_cases(self):
        invalid = copy.deepcopy(self.cases)
        invalid[-1]['expected'][0]['attribution'] = []
        self.path.write_text(json.dumps({'cases': invalid}))
        with self.assertRaises(MemoryError):
            evaluate(self.path, self.adapter, 'fixture', case_id=self.cases[0]['id'])
        self.assertEqual(self.requests, [])

    def test_generation_limit_is_forwarded_and_changes_adapter_identity(self):
        adapter = LocalModelAdapter(self.adapter.endpoint, 'fixture-model', max_tokens=512)
        result = evaluate(self.path, adapter, 'fixture')
        self.assertTrue(result['automated_checks_passed'])
        self.assertTrue(all(r['max_tokens'] == 512 for r in self.requests))
        self.assertEqual(result['generation_max_tokens_requested'], 512)
        self.assertNotEqual(adapter.identity, self.adapter.identity)
        other = LocalModelAdapter(self.adapter.endpoint, 'fixture-model', max_tokens=1024)
        self.assertNotEqual(adapter.identity, other.identity)

    def test_default_request_and_continue_on_error_remain_unchanged(self):
        self.mode = 'invented'
        result = evaluate(self.path, self.adapter, 'fixture')
        self.assertEqual(len(self.requests), 5)
        self.assertTrue(all('max_tokens' not in r for r in self.requests))
        self.assertEqual(self.adapter.identity, 'local-openai-v1:' + self.adapter.endpoint + ':fixture-model')
        self.assertIsNone(result['generation_max_tokens_requested'])
        self.assertFalse(result['stop_on_error'])
        self.assertIsNone(result['stopped_reason'])
        self.assertTrue(result['all_cases_attempted'])
        self.assertFalse(result['automated_checks_passed'])

    def test_truncation_is_not_accepted_even_when_json_is_valid(self):
        self.mode = 'truncated'
        adapter = LocalModelAdapter(self.adapter.endpoint, 'fixture-model', max_tokens=512)
        result = evaluate(self.path, adapter, 'fixture', stop_on_error=True)
        self.assertEqual(len(self.requests), 1)
        self.assertIn('error', result['cases'][0])
        self.assertFalse(result['automated_checks_passed'])
        self.assertEqual(len(result['not_run_case_ids']), 4)

    def test_invalid_generation_limits_are_rejected_without_http(self):
        for value in (True, False, 0, -1, 1.5, '512'):
            with self.subTest(value=value), self.assertRaises(MemoryError):
                LocalModelAdapter(self.adapter.endpoint, 'fixture-model', max_tokens=value)
        self.assertEqual(self.requests, [])

    def test_cli_single_case_exit_only_describes_selected_case(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['--cases', str(self.path), '--endpoint', self.adapter.endpoint,
                         '--model', 'fixture-model', '--model-revision', 'fixture',
                         '--case-id', self.cases[0]['id'], '--max-tokens', '512', '--stop-on-error'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(result['selection_checks_passed'])
        self.assertFalse(result['automated_checks_passed'])
        self.assertFalse(result['all_cases_attempted'])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]['max_tokens'], 512)

    def test_cli_stopped_run_exits_two_and_keeps_failure_report(self):
        self.mode = 'invented'
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['--cases', str(self.path), '--endpoint', self.adapter.endpoint,
                         '--model', 'fixture-model', '--model-revision', 'fixture', '--stop-on-error'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result['stopped_reason'], 'case_error')
        self.assertIn('error', result['cases'][0])

    def test_cli_rejects_invalid_options_before_http(self):
        for options in (['--max-tokens', '0'], ['--case-id', 'absent']):
            with self.subTest(options=options), redirect_stderr(io.StringIO()):
                code = main(['--cases', str(self.path), '--endpoint', self.adapter.endpoint,
                             '--model', 'fixture-model', '--model-revision', 'fixture', *options])
            self.assertEqual(code, 2)
        self.assertEqual(self.requests, [])

    # --- what the report can evidence about the generation itself -----------------------

    def test_the_stop_reason_and_token_counts_are_kept_for_a_successful_case(self):
        """Without these a report cannot show whether the server acted on the token limit."""
        adapter = LocalModelAdapter(self.adapter.endpoint, 'fixture-model', max_tokens=512)
        report = evaluate(self.path, adapter, 'rev', case_id=self.cases[0]['id'])
        row = report['cases'][0]
        self.assertTrue(row['classification_match'])
        self.assertEqual(row['finish_reason'], 'stop')
        self.assertEqual(row['usage'], {'prompt_tokens': 12, 'completion_tokens': 34,
                                        'total_tokens': 46})
        self.assertEqual(report['finish_reasons'], ['stop'])
        self.assertEqual(report['generation_max_tokens_requested'], 512)
        self.assertEqual(report['failure_kinds'], [])

    def test_a_truncated_generation_is_distinguishable_from_a_malformed_one(self):
        self.mode = 'truncated'
        adapter = LocalModelAdapter(self.adapter.endpoint, 'fixture-model', max_tokens=512)
        truncated = evaluate(self.path, adapter, 'rev', case_id=self.cases[0]['id'])
        self.mode = 'invented'
        malformed = evaluate(self.path, LocalModelAdapter(self.adapter.endpoint, 'fixture-model'),
                             'rev', case_id=self.cases[0]['id'])
        self.assertEqual(truncated['cases'][0]['failure_kind'], FAILED_TRUNCATED)
        self.assertEqual(malformed['cases'][0]['failure_kind'], FAILED_RESPONSE)
        self.assertNotEqual(truncated['cases'][0]['error'], malformed['cases'][0]['error'])
        self.assertEqual(truncated['failure_kinds'], [FAILED_TRUNCATED])
        self.assertEqual(truncated['cases'][0]['finish_reason'], 'length')

    def test_a_server_that_truncates_without_being_asked_is_still_refused(self):
        """Whoever set the limit, an answer built on a cut-off response is not grounded."""
        self.mode = 'truncated-no-limit'
        report = evaluate(self.path, self.adapter, 'rev', case_id=self.cases[0]['id'])
        row = report['cases'][0]
        self.assertFalse(row['schema_valid'])
        self.assertEqual(row['failure_kind'], FAILED_TRUNCATED)
        self.assertIsNone(report['generation_max_tokens_requested'])

    def test_only_non_negative_integer_counts_are_kept_and_nothing_else(self):
        """The proposals legitimately quote the source; the observation must carry no text."""
        self.mode = 'odd-usage'
        report = evaluate(self.path, self.adapter, 'rev', case_id=self.cases[0]['id'])
        row = report['cases'][0]
        self.assertEqual(row['usage'], {'prompt_tokens': 12})   # -3, 'many' and the extra dropped
        observation = json.dumps({'finish_reason': row['finish_reason'], 'usage': row['usage']},
                                 ensure_ascii=False)
        self.assertNotIn('secret_note', observation)
        self.assertNotIn(self.cases[0]['inbox'], observation)

    def test_an_unusable_finish_reason_is_dropped_rather_than_copied_in(self):
        self.mode = 'long-finish-reason'
        report = evaluate(self.path, self.adapter, 'rev', case_id=self.cases[0]['id'])
        row = report['cases'][0]
        self.assertIsNone(row['finish_reason'])
        self.assertEqual(report['finish_reasons'], [])
        observation = json.dumps({'finish_reason': row['finish_reason'], 'usage': row['usage']},
                                 ensure_ascii=False)
        self.assertNotIn(self.cases[0]['inbox'], observation)

    def test_a_response_without_usage_or_finish_reason_records_neither(self):
        self.mode = 'no-usage'
        report = evaluate(self.path, self.adapter, 'rev', case_id=self.cases[0]['id'])
        self.assertEqual(report['cases'][0]['usage'], {})
        self.mode = 'no-finish-reason'
        report = evaluate(self.path, self.adapter, 'rev', case_id=self.cases[1]['id'])
        self.assertIsNone(report['cases'][0]['finish_reason'])

    def test_an_observation_from_one_case_does_not_leak_into_the_next(self):
        self.mode = 'second-invalid'
        report = evaluate(self.path, self.adapter, 'rev')
        first, second = report['cases'][0], report['cases'][1]
        self.assertTrue(first['classification_match'])
        self.assertEqual(second['failure_kind'], FAILED_RESPONSE)
        self.assertEqual(second['finish_reason'], 'stop')     # its own response, not the first's
        self.assertEqual(len(report['cases']), len(self.cases))

    # --- the stop reason is an allowlist, not a length limit ----------------------------

    def one(self, case_index=0, **kwargs):
        adapter = LocalModelAdapter(self.adapter.endpoint, 'fixture-model', **kwargs)
        report = evaluate(self.path, adapter, 'rev', case_id=self.cases[case_index]['id'])
        return report, report['cases'][0]

    def observation_text(self, row):
        return json.dumps({k: row.get(k) for k in
                           ('finish_reason', 'finish_reason_state', 'usage', 'generation_budget')},
                          ensure_ascii=False)

    def test_a_recognised_stop_reason_is_recorded_as_known(self):
        report, row = self.one(max_tokens=512)
        self.assertEqual(row['finish_reason'], 'stop')
        self.assertEqual(row['finish_reason_state'], FINISH_KNOWN)
        self.assertIn('stop', FINISH_REASONS)
        self.assertEqual(report['finish_reason_states'], [FINISH_KNOWN])

    def test_a_short_unrecognised_reason_is_not_copied_into_the_record(self):
        """A length limit let response-derived text through; only the allowlist is recorded."""
        self.mode = 'short-unknown-finish-reason'
        report, row = self.one()
        self.assertIsNone(row['finish_reason'])
        self.assertEqual(row['finish_reason_state'], FINISH_UNKNOWN)
        self.assertEqual(report['finish_reasons'], [])
        self.assertNotIn(self.cases[0]['inbox'][:12], self.observation_text(row))

    def test_a_long_unrecognised_reason_is_also_dropped(self):
        self.mode = 'long-finish-reason'
        report, row = self.one()
        self.assertEqual(row['finish_reason_state'], FINISH_UNKNOWN)
        self.assertNotIn(self.cases[0]['inbox'], self.observation_text(row))

    def test_the_four_reason_states_are_distinguishable(self):
        seen = {}
        for mode, case_index, expected in (('valid', 0, FINISH_KNOWN),
                                           ('short-unknown-finish-reason', 1, FINISH_UNKNOWN),
                                           ('invalid-finish-reason', 2, FINISH_INVALID),
                                           ('null-finish-reason', 3, FINISH_ABSENT),
                                           ('no-finish-reason', 4, FINISH_ABSENT)):
            self.mode = mode
            _, row = self.one(case_index)
            seen[mode] = row['finish_reason_state']
            self.assertEqual(row['finish_reason_state'], expected, mode)
            if expected is not FINISH_KNOWN:
                self.assertIsNone(row['finish_reason'])
        self.assertEqual(len({FINISH_KNOWN, FINISH_UNKNOWN, FINISH_INVALID, FINISH_ABSENT}), 4)

    def test_only_non_negative_plain_integers_survive_in_usage(self):
        self.mode = 'bad-usage-types'
        _, row = self.one()
        self.assertEqual(row['usage'], {'reasoning_tokens': 7})   # True, -1 and 1.5 all dropped

    # --- the budget observation is separate from the classification result ---------------

    def test_a_matching_classification_does_not_assert_the_budget_held(self):
        """completion_tokens above the requested limit still leaves the checks passing."""
        self.mode = 'over-budget'
        report, row = self.one(max_tokens=512)
        self.assertTrue(row['classification_match'])
        self.assertTrue(report['selection_checks_passed'])
        self.assertEqual(row['generation_budget']['observation'], BUDGET_OVER)
        self.assertEqual(row['generation_budget']['completion_tokens_reported'], 513)
        self.assertEqual(report['generation_budget_observations'], [BUDGET_OVER])

    def test_a_missing_token_count_leaves_the_budget_unobservable(self):
        self.mode = 'no-completion-tokens'
        report, row = self.one(max_tokens=512)
        self.assertTrue(report['selection_checks_passed'])
        self.assertEqual(row['generation_budget']['observation'], BUDGET_UNOBSERVABLE)
        self.assertIsNone(row['generation_budget']['completion_tokens_reported'])

    def test_a_count_inside_the_limit_is_recorded_as_an_observation_only(self):
        report, row = self.one(max_tokens=512)
        budget = row['generation_budget']
        self.assertEqual(budget['observation'], BUDGET_WITHIN)
        self.assertEqual(budget['max_tokens_requested'], 512)
        self.assertIn('not an enforced limit', budget['source'])

    def test_without_a_requested_limit_the_budget_is_not_observable(self):
        _, row = self.one()
        self.assertEqual(row['generation_budget']['observation'], BUDGET_UNOBSERVABLE)
        self.assertIsNone(row['generation_budget']['max_tokens_requested'])

    def test_a_truncated_run_reports_length_without_asserting_the_limit_was_honoured(self):
        self.mode = 'truncated'
        report, row = self.one(max_tokens=512)
        self.assertEqual(row['finish_reason'], 'length')
        self.assertEqual(row['finish_reason_state'], FINISH_KNOWN)
        self.assertEqual(row['failure_kind'], FAILED_TRUNCATED)
        self.assertFalse(row['schema_valid'])
        # length says the server declared a cut-off, not that it respected the figure asked for.
        self.assertIn(row['generation_budget']['observation'],
                      (BUDGET_WITHIN, BUDGET_OVER, BUDGET_UNOBSERVABLE))


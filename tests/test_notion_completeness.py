import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from knowledge_memory import notion_api as api


def failure(code, retry=None):
    headers = {} if retry is None else {'Retry-After': retry}
    return HTTPError('https://example.invalid/private', code, 'private detail',
                     headers, io.BytesIO(b'private response body'))


class RetryTests(unittest.TestCase):
    def run_request(self, responses):
        with patch.object(api.urllib.request, 'urlopen', side_effect=responses) as calls, \
                patch.object(api.time, 'sleep') as sleep:
            try:
                result = api._request('https://example.invalid', 'secret', api.NOTION_VERSION,
                                      'GET', '/private')
            except api.NotionError as exc:
                result = exc
            return result, calls.call_count, [x.args[0] for x in sleep.call_args_list]

    def test_retry_after_is_respected_for_429_and_529(self):
        for code in (429, 529):
            result, calls, waits = self.run_request([failure(code, '7'), io.BytesIO(b'{}')])
            self.assertEqual((result, calls, waits), ({}, 2, [7]))

    def test_no_early_retry_or_final_sleep(self):
        result, calls, waits = self.run_request([failure(429, '60')])
        self.assertEqual((calls, waits), (1, []))
        self.assertEqual(result.retry_after_seconds, 60)
        self.assertIsNotNone(result.retry_at)
        result, calls, waits = self.run_request([failure(529) for _ in range(3)])
        self.assertEqual((calls, waits), (3, [1, 2]))
        self.assertIsInstance(result, api.NotionError)

    def test_total_wait_budget_and_invalid_header_fail_closed(self):
        result, calls, waits = self.run_request([failure(429, '20'), failure(429, '20')])
        self.assertEqual((calls, waits), (2, [20]))
        self.assertEqual(result.retry_after_seconds, 20)
        for value in ('invalid', '-1', 'nan', 'inf'):
            result, calls, waits = self.run_request([failure(429, value)])
            self.assertEqual((calls, waits), (1, []))
            self.assertIn('retry_later', str(result))

    def test_permanent_errors_do_not_retry_or_echo_private_data(self):
        for code in (400, 401, 403, 404):
            result, calls, waits = self.run_request([failure(code)])
            self.assertEqual((calls, waits), (1, []))
            for private in ('private', 'secret', 'example.invalid'):
                self.assertNotIn(private, str(result))

    def test_wait_budget_also_covers_transport_and_invalid_json(self):
        for second in (URLError('private detail'), TimeoutError('private detail'),
                       io.BytesIO(b'not JSON'), io.BytesIO(b'\xff')):
            with self.subTest(second=type(second).__name__):
                result, calls, waits = self.run_request(
                    [failure(429, '30'), second, io.BytesIO(b'{}')])
                self.assertEqual((calls, waits), (2, [30]))
                self.assertIsInstance(result, api.NotionError)
                self.assertEqual(result.retry_after_seconds, 2)
                self.assertIsNotNone(result.retry_at)
                self.assertNotIn('private', str(result))
        result, calls, waits = self.run_request(
            [failure(429, '28'), io.BytesIO(b'not JSON'), io.BytesIO(b'{}')])
        self.assertEqual((result, calls, waits), ({}, 3, [28, 2]))


class CompletenessTests(unittest.TestCase):
    def test_incomplete_on_first_middle_or_last_page(self):
        for index in range(3):
            pages = [{'results': [n], 'has_more': n < 2,
                      'next_cursor': str(n + 1) if n < 2 else None} for n in range(3)]
            pages[index]['request_status'] = {'type': 'incomplete'}
            call = unittest.mock.Mock(side_effect=pages)
            with self.assertRaisesRegex(api.NotionError, 'incomplete'):
                api._paginate(call, '/data_sources/example/query', {})
            self.assertEqual(call.call_count, index + 1)

    def test_complete_status_and_normal_pagination(self):
        call = unittest.mock.Mock(side_effect=[
            {'results': [1], 'has_more': True, 'next_cursor': 'next',
             'request_status': {'type': 'complete'}},
            {'results': [2], 'has_more': False}])
        self.assertEqual(api._paginate(call, '/query', {}), [1, 2])

    def fetch(self, responses, **kwargs):
        with patch.object(api, '_request', side_effect=responses) as call:
            result = api.fetch_snapshot('fixture-token', self.out, **kwargs)
            return result, [c.args[3:5] for c in call.call_args_list]

    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / 'snapshot'
        self.empty = {'results': [], 'has_more': False}

    def test_database_resolves_one_source_and_preserves_both_ids(self):
        result, calls = self.fetch([{'data_sources': [{'id': 'ds'}]},
                                    {'properties': {}, 'parent': {'database_id': 'db'}}, self.empty],
                                   database='db')
        self.assertEqual((result['database_id'], result['data_source_id']), ('db', 'ds'))
        self.assertEqual(calls, [('GET', '/databases/db'), ('GET', '/data_sources/ds'),
                                 ('POST', '/data_sources/ds/query')])

    def test_multiple_or_missing_sources_require_explicit_selection(self):
        for sources in ([], [{'id': 'a'}, {'id': 'b'}], None):
            with patch.object(api, '_request', return_value={'data_sources': sources}) as call:
                with self.assertRaisesRegex(api.NotionError, '--data-source'):
                    api.fetch_snapshot('fixture', self.out, database='db')
                self.assertEqual(call.call_count, 1)
                self.assertFalse(self.out.exists())

    def test_versions_are_explicit(self):
        _, calls = self.fetch([{'properties': {}}, self.empty], database='db', version='2022-06-28')
        self.assertEqual(calls[-1], ('POST', '/databases/db/query'))
        with patch.object(api, '_request') as call:
            for kwargs in ({'data_source': 'ds', 'version': '2022-06-28'},
                           {'database': 'db', 'version': '2099-01-01'}):
                with self.assertRaises(api.NotionError):
                    api.fetch_snapshot('fixture', self.out, **kwargs)
            call.assert_not_called()

    def test_failed_query_keeps_previous_snapshot_bytes(self):
        self.fetch([{'properties': {}}, self.empty], data_source='ds')
        before = {p.name: p.read_bytes() for p in self.out.iterdir()}
        incomplete = dict(self.empty, request_status={'type': 'incomplete'})
        with self.assertRaises(api.NotionError):
            self.fetch([{'properties': {}}, incomplete], data_source='ds')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.out.iterdir()})

    def test_initial_fetch_keeps_ten_thousand_result_guard(self):
        with patch.object(api, '_request', return_value={'properties': {}}), \
                patch.object(api, '_paginate', return_value=[{}] * 10_000):
            with self.assertRaisesRegex(api.NotionError, 'enumeration_limit'):
                api.fetch_snapshot('fixture', self.out, data_source='ds')
        self.assertFalse(self.out.exists())

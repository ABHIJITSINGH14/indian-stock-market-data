"""Offline failure-contract tests. No test contacts a market-data provider."""
import json
import runpy
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

from utils.api_client import APIClient
from utils.data_processor import DataProcessor
from scripts.bulk_block_deals import BulkBlockDealsDownloader

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 17)
NEXT = date(2026, 9, 18)
CSV = 'Date,Symbol,Quantity\n2026-09-17,CI_FIXTURE_ONLY,1\n'


def response(status=200, text=CSV):
    result = requests.Response()
    result.status_code = status
    result._content = text.encode()
    result.encoding = 'utf-8'
    result.url = 'https://example.invalid/data'
    return result


def http_error(status):
    return requests.HTTPError(f'HTTP {status}', response=response(status))


class APIContractTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch('utils.api_client.time.sleep').start()
        self.addCleanup(patch.stopall)
        self.client = APIClient()
        self.get = Mock()
        self.client.session.get = self.get
        self.addCleanup(self.client.close)

    def test_success(self):
        value = response()
        self.get.return_value = value
        self.assertIs(self.client.get(value.url), value)
        self.assertEqual(self.get.call_count, 1)

    def test_retry_false_really_uses_one_attempt(self):
        self.get.side_effect = requests.Timeout('timeout')
        with self.assertLogs('utils.api_client', level='ERROR') as logs:
            with self.assertRaises(requests.Timeout):
                self.client.get('https://example.invalid/data', retry=False)
        self.assertEqual(self.get.call_count, 1)
        self.assertIn('after 1 attempt(s)', '\n'.join(logs.output))

    def test_transient_timeout_retry_bound(self):
        self.get.side_effect = requests.Timeout('timeout')
        with patch('utils.api_client.API_RETRY_ATTEMPTS', 3):
            with self.assertRaises(requests.Timeout):
                self.client.get('https://example.invalid/data')
        self.assertEqual(self.get.call_count, 3)

    def test_transient_server_error_can_recover(self):
        self.get.side_effect = [response(503), response()]
        self.assertEqual(self.client.get('https://example.invalid/data').status_code, 200)
        self.assertEqual(self.get.call_count, 2)

    def test_rate_limit_not_retried(self):
        self.get.return_value = response(429)
        with self.assertRaises(requests.HTTPError):
            self.client.get('https://example.invalid/data')
        self.assertEqual(self.get.call_count, 1)

    def test_access_denial_not_retried(self):
        self.get.return_value = response(403)
        with self.assertRaises(requests.HTTPError):
            self.client.get('https://example.invalid/data')
        self.assertEqual(self.get.call_count, 1)

    def test_missing_resource_not_retried(self):
        self.get.return_value = response(404)
        with self.assertRaises(requests.HTTPError):
            self.client.get('https://example.invalid/data')
        self.assertEqual(self.get.call_count, 1)

    def test_tls_error_not_retried(self):
        self.get.side_effect = requests.exceptions.SSLError('TLS rejected')
        with self.assertRaises(requests.exceptions.SSLError):
            self.client.get('https://example.invalid/data')
        self.assertEqual(self.get.call_count, 1)

    def test_tls_verification_is_not_disabled(self):
        self.get.return_value = response()
        self.client.get('https://example.invalid/data')
        self.assertNotEqual(self.get.call_args.kwargs.get('verify'), False)
        self.assertTrue(self.client.session.verify)
        self.assertGreater(self.get.call_args.kwargs['timeout'], 0)


class DealsContractTests(unittest.TestCase):
    def setUp(self):
        self.collector = BulkBlockDealsDownloader()
        self.collector.client.get = Mock(return_value=response())
        self.addCleanup(self.collector.client.close)

    def test_no_frames_is_not_success(self):
        self.assertFalse(self.collector.save_data())

    def test_both_fetches_fail_is_not_success(self):
        with patch.object(self.collector, 'fetch_bulk_deals', return_value=False), \
             patch.object(self.collector, 'fetch_block_deals', return_value=False):
            self.assertFalse(self.collector.run())

    def test_live_tls_failure_stops_dates_and_second_family(self):
        self.collector.client.get.side_effect = requests.exceptions.SSLError('TLS rejected')
        self.assertFalse(self.collector.run())
        self.assertEqual(self.collector.client.get.call_count, 1)
        self.assertTrue(self.collector.source_blocked)

    def test_live_rate_limit_stops_dates_and_second_family(self):
        self.collector.client.get.side_effect = http_error(429)
        self.assertFalse(self.collector.run())
        self.assertEqual(self.collector.client.get.call_count, 1)

    def test_404_is_unverified_not_no_deals(self):
        self.collector.client.get.side_effect = [http_error(404), response()]
        self.assertFalse(self.collector.fetch_bulk_deals(DAY, NEXT))
        self.assertEqual(len(self.collector.bulk_deals), 1)
        self.assertFalse(self.collector.source_blocked)

    def test_html_response_not_admitted_as_csv(self):
        self.collector.client.get.return_value = response(text='<html>Access denied</html>')
        self.assertFalse(self.collector.fetch_bulk_deals(DAY, NEXT))
        self.assertTrue(self.collector.bulk_deals.empty)
        self.assertEqual(self.collector.client.get.call_count, 1)

    def test_header_only_window_is_not_claimed_complete(self):
        self.collector.client.get.return_value = response(text='Date,Symbol,Quantity\n')
        self.assertFalse(self.collector.fetch_bulk_deals(DAY, DAY))

    def test_missing_response_is_failure(self):
        self.collector.client.get.return_value = None
        self.assertFalse(self.collector.fetch_bulk_deals(DAY, NEXT))
        self.assertEqual(self.collector.client.get.call_count, 1)

    def test_empty_body_is_failure(self):
        self.collector.client.get.return_value = response(text='')
        self.assertFalse(self.collector.fetch_bulk_deals(DAY, NEXT))

    def test_successful_window(self):
        self.assertTrue(self.collector.fetch_bulk_deals(DAY, NEXT))
        self.assertEqual(len(self.collector.bulk_deals), 2)

    def test_invalid_range(self):
        with self.assertRaises(ValueError):
            self.collector.fetch_bulk_deals(NEXT, DAY)
        self.collector.client.get.assert_not_called()

    def test_unexpected_programming_error_is_not_swallowed(self):
        self.collector.client.get.side_effect = RuntimeError('programming defect')
        with self.assertRaises(RuntimeError):
            self.collector.fetch_bulk_deals(DAY, DAY)

    def test_partial_fetch_is_saved_but_still_failure(self):
        frame = pd.DataFrame({'Date': ['2026-09-17'], 'Symbol': ['CI_FIXTURE_ONLY']})
        def bulk(*args):
            self.collector.bulk_deals = frame.copy()
            return False
        def block(*args):
            self.collector.block_deals = frame.copy()
            return True
        with patch.object(self.collector, 'fetch_bulk_deals', side_effect=bulk), \
             patch.object(self.collector, 'fetch_block_deals', side_effect=block), \
             patch.object(self.collector.processor, 'save_csv', return_value=True) as save:
            self.assertFalse(self.collector.run())
            self.assertEqual(save.call_count, 2)

    def test_write_failure_propagates(self):
        self.collector.bulk_deals = pd.DataFrame({'Date': ['2026-09-17'], 'Symbol': ['CI_FIXTURE_ONLY']})
        with patch.object(self.collector.processor, 'save_csv', return_value=False):
            self.assertFalse(self.collector.save_data())

    def test_both_complete_and_saved_is_success(self):
        frame = pd.DataFrame({'Date': ['2026-09-17'], 'Symbol': ['CI_FIXTURE_ONLY']})
        def bulk(*args):
            self.collector.bulk_deals = frame.copy()
            return True
        def block(*args):
            self.collector.block_deals = frame.copy()
            return True
        with patch.object(self.collector, 'fetch_bulk_deals', side_effect=bulk), \
             patch.object(self.collector, 'fetch_block_deals', side_effect=block), \
             patch.object(self.collector.processor, 'save_csv', return_value=True):
            self.assertTrue(self.collector.run())


class RawEvidenceTests(unittest.TestCase):
    def test_missing_values_never_copied_between_companies(self):
        source = pd.DataFrame({'symbol': ['A', 'B', 'C'], 'market_cap': [10.0, None, 30.0]})
        cleaned = DataProcessor.clean_data(source)
        self.assertTrue(pd.isna(cleaned.loc[1, 'market_cap']))

    def test_missing_prices_never_backfilled_from_future(self):
        source = pd.DataFrame({'symbol': ['A', 'A'], 'close': [None, 30.0]})
        self.assertTrue(pd.isna(DataProcessor.clean_data(source).loc[0, 'close']))

    def test_input_not_mutated(self):
        source = pd.DataFrame({'symbol': ['A'], 'close': [1.0]})
        cleaned = DataProcessor.clean_data(source)
        cleaned.loc[0, 'close'] = 2.0
        self.assertEqual(source.loc[0, 'close'], 1.0)

    def test_duplicate_rows_removed(self):
        self.assertEqual(len(DataProcessor.clean_data(pd.DataFrame({'x': [1, 1]}))), 1)

    def test_none_and_empty_preserved(self):
        self.assertIsNone(DataProcessor.clean_data(None))
        self.assertTrue(DataProcessor.clean_data(pd.DataFrame()).empty)

    def test_null_values_rejected_by_validation(self):
        self.assertFalse(DataProcessor.validate_data(pd.DataFrame({'x': [None]})))


class FundamentalsContractTests(unittest.TestCase):
    def exercise_status(self, status):
        provider = Mock()
        provider.Ticker.side_effect = http_error(status)
        with patch.dict(sys.modules, {'yfinance': provider}):
            namespace = runpy.run_path(
                str(ROOT / 'scripts/download_fundamentals.py'),
                run_name='contract_test_fundamentals',
            )
        task = namespace['FundamentalsDownloader']()
        if status in (401, 403, 429):
            with self.assertRaises(requests.HTTPError):
                task.download_all_fundamentals()
            self.assertEqual(provider.Ticker.call_count, 1)
        else:
            self.assertIsNone(task.fetch_stock_info('CI_FIXTURE_ONLY'))

    def test_rate_limit_stops_ticker_loop(self):
        self.exercise_status(429)

    def test_access_denial_stops_ticker_loop(self):
        self.exercise_status(403)

    def test_authentication_failure_stops_ticker_loop(self):
        self.exercise_status(401)

    def test_symbol_not_found_remains_a_symbol_failure(self):
        self.exercise_status(404)


class OrchestrationSummaryTests(unittest.TestCase):
    def exercise(self, first_result):
        """Inject task doubles; exercise the real orchestrator, not provider clients."""
        modules = {}
        specs = [
            ('download_nse_data', 'NSEDataDownloader'),
            ('download_bse_data', 'BSEDataDownloader'),
            ('download_fundamentals', 'FundamentalsDownloader'),
            ('bulk_block_deals', 'BulkBlockDealsDownloader'),
            ('corporate_actions', 'CorporateActionsDownloader'),
        ]
        for i, (name, cls) in enumerate(specs):
            module = types.ModuleType('scripts.' + name)
            task = Mock()
            if i == 0 and isinstance(first_result, Exception):
                task.run.side_effect = first_result
            else:
                task.run.return_value = first_result if i == 0 else True
            setattr(module, cls, Mock(return_value=task))
            modules['scripts.' + name] = module
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict(sys.modules, modules), \
             patch('config.config.LOG_DIR', temporary), \
             patch('time.sleep'), \
             patch.dict('os.environ', {'GITHUB_RUN_ID': '123', 'GITHUB_SHA': 'test-sha'}):
            namespace = runpy.run_path(str(ROOT / 'scripts/run_all.py'), run_name='contract_test')
            outcome = namespace['run_all_downloads']()
            manifest = json.loads(Path(temporary, 'download-summary.json').read_text())
            return outcome, manifest

    def test_failed_task_is_manifested(self):
        outcome, manifest = self.exercise(False)
        self.assertFalse(outcome)
        self.assertFalse(manifest['all_tasks_success'])
        self.assertEqual(manifest['tasks']['NSE Historical Data']['status'], 'FAILED')
        self.assertEqual(manifest['run_id'], '123')
        self.assertEqual(manifest['commit_sha'], 'test-sha')

    def test_exception_is_manifested(self):
        outcome, manifest = self.exercise(RuntimeError('source unavailable'))
        self.assertFalse(outcome)
        self.assertEqual(manifest['tasks']['NSE Historical Data']['status'], 'ERROR')

    def test_task_success_is_not_historical_completeness(self):
        outcome, manifest = self.exercise(True)
        self.assertTrue(outcome)
        self.assertTrue(manifest['all_tasks_success'])
        self.assertEqual(manifest['historical_completeness'], 'not_verified')


if __name__ == '__main__':
    unittest.main()

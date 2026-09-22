"""Archive routing preserves obligations, never converts unknown failures into skips."""
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import requests

from config.issuer_identity import historical_price_deferral, HDFC_PRICE_REVIEWED_ON
from test_yahoo_sources import load_adapter, prices, YFRateLimitError
from test_price_continuation import mixed


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 22, 4, 30, tzinfo=timezone.utc)


class PriceArchiveRoutingTests(unittest.TestCase):
    def setUp(self):
        self.cls, self.provider = load_adapter('download_nse_data.py', 'NSEDataDownloader')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = patch.dict(self.cls.download_all_stocks.__globals__, {'datetime': FrozenDateTime})
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.sleep = patch('time.sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.provider.download.return_value = prices()

    def task(self, symbols=None, enabled=True, continuation=True):
        return self.cls(symbols=symbols or ['HDFC.NS', 'ICICIBANK.NS'],
                        start_date='2026-09-14', end_date='2026-09-19',
                        output_path=Path(self.temporary.name, 'prices.csv'),
                        defer_archival_prices=enabled,
                        continue_after_row_rejection=continuation)

    def verify_manifest(self, task):
        record = task.coverage['archival_plan']
        payload = Path(record['path']).read_bytes()
        self.assertEqual(record['bytes'], len(payload))
        self.assertEqual(record['sha256'], hashlib.sha256(payload).hexdigest())
        manifest = json.loads(payload)
        self.assertEqual(manifest['kind'], 'outstanding_price_archival_work_not_market_data')
        self.assertEqual(manifest['original_requested'], task.symbols)
        self.assertEqual(manifest['downloaded_rows'], 0)
        self.assertEqual(manifest['archival_requests'], task.coverage['archival_requests'])
        self.assertEqual(manifest['historical_completeness'], 'not_verified')
        self.assertFalse(Path(record['path'] + '.tmp').exists())
        return manifest

    def test_policy_requires_reviewed_exact_identity_not_empty_response(self):
        for symbol in ('HDFCBANK.NS', 'UNKNOWN.NS', 'HDFC.BO', 'INFY.NS'):
            self.assertIsNone(historical_price_deferral(
                symbol, date(2000, 1, 1), date(2026, 9, 22), HDFC_PRICE_REVIEWED_ON))
        self.assertIsNone(historical_price_deferral(
            'HDFC.NS', date(2000, 1, 1), date(2026, 9, 22), date(2026, 9, 21)))

    def test_requested_interval_never_clipped_or_successor_spliced(self):
        for start, end in [('2000-01-01', '2023-06-30'), ('2000-01-01', '2026-09-22'),
                           ('2023-07-01', '2023-07-13'), ('2024-01-01', '2024-02-01')]:
            item = historical_price_deferral('HDFC.NS', date.fromisoformat(start),
                                              date.fromisoformat(end), HDFC_PRICE_REVIEWED_ON)
            self.assertEqual(item['requested_start'], start)
            self.assertEqual(item['requested_end_exclusive'], end)
            self.assertIsNone(item['last_trading_date'])
            self.assertIsNone(item['replacement_symbol'])
            self.assertIsNone(item['archive_source'])
            self.assertEqual(item['archive_fetch_status'], 'awaiting_verified_source')
            self.assertTrue(item['historical_obligation_preserved'])
            self.assertEqual(len(item['evidence_urls']), 2)

    def test_policy_returns_fresh_records_and_rejects_invalid_interval(self):
        args = ('HDFC.NS', date(2020, 1, 1), date(2026, 9, 22), HDFC_PRICE_REVIEWED_ON)
        historical_price_deferral(*args)['evidence_urls'].clear()
        self.assertEqual(len(historical_price_deferral(*args)['evidence_urls']), 2)
        with self.assertRaises(ValueError):
            historical_price_deferral('HDFC.NS', date(2026, 1, 1), date(2026, 1, 1), HDFC_PRICE_REVIEWED_ON)

    def test_direct_default_still_requests_hdfc_and_stops_on_empty(self):
        task = self.cls(['HDFC.NS', 'ICICIBANK.NS'], '2026-09-14', '2026-09-19',
                        Path(self.temporary.name, 'prices.csv'))
        self.provider.download.side_effect = [pd.DataFrame(), prices()]
        self.assertFalse(task.run())
        self.assertEqual([c.args[0] for c in self.provider.download.call_args_list], ['HDFC.NS'])
        self.assertEqual(task.coverage['deferred_archival'], [])
        self.assertIsNone(task.coverage['archival_plan'])
        self.assertEqual(task.coverage['pending_provider'], ['ICICIBANK.NS'])

    def test_only_archival_makes_no_request_but_writes_outstanding_work(self):
        task = self.task(['HDFC.NS'])
        self.assertFalse(task.run())
        self.provider.download.assert_not_called()
        self.assertFalse(task.output_path.exists())
        self.assertEqual(task.coverage['returned'], [])
        self.assertEqual(task.coverage['not_returned'], ['HDFC.NS'])
        self.assertEqual(task.coverage['not_attempted'], ['HDFC.NS'])
        self.assertEqual(task.coverage['deferred_archival'], ['HDFC.NS'])
        self.assertEqual(task.coverage['pending_provider'], [])
        self.verify_manifest(task)

    def test_later_company_is_reached_without_claiming_scope_complete(self):
        task = self.task()
        with patch.dict('os.environ', {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
                                      'GITHUB_SHA': 'a' * 40}):
            self.assertFalse(task.run())
        self.assertEqual([c.args[0] for c in self.provider.download.call_args_list], ['ICICIBANK.NS'])
        self.assertEqual(task.coverage['requested'], ['HDFC.NS', 'ICICIBANK.NS'])
        self.assertEqual(task.coverage['returned'], ['ICICIBANK.NS'])
        self.assertEqual(task.coverage['not_returned'], ['HDFC.NS'])
        self.assertEqual(task.coverage['pending_provider'], [])
        self.assertEqual(set(pd.read_csv(task.output_path)['provider_symbol']), {'ICICIBANK.NS'})
        manifest = self.verify_manifest(task)
        self.assertEqual([manifest[k] for k in ('run_id', 'run_attempt', 'commit_sha')], ['123', '2', 'a' * 40])

    def test_all_original_twenty_identities_remain_nineteen_requests_not_twenty_successes(self):
        task = self.task(self.cls.NSE_STOCKS)
        self.assertFalse(task.run())
        expected = [s for s in task.symbols if s != 'HDFC.NS']
        self.assertEqual([c.args[0] for c in self.provider.download.call_args_list], expected)
        self.assertEqual(len(expected), 19)
        self.assertEqual(len(task.coverage['requested']), 20)
        self.assertEqual(task.coverage['returned'], expected)
        self.assertEqual(task.coverage['not_returned'], ['HDFC.NS'])
        self.assertEqual(task.coverage['not_attempted'], ['HDFC.NS'])
        self.assertEqual(task.coverage['pending_provider'], [])
        self.assertEqual(len(pd.read_csv(task.output_path)), 38)

    def test_plan_persisted_before_any_provider_call(self):
        task = self.task(['RELIANCE.NS', 'HDFC.NS', 'ICICIBANK.NS'])
        def check_before_call(*args, **kwargs):
            self.verify_manifest(task)
            return prices()
        self.provider.download.side_effect = check_before_call
        self.assertFalse(task.run())
        self.assertEqual(self.provider.download.call_count, 2)

    def test_early_empty_response_keeps_archive_plan_and_stops_other_requests(self):
        task = self.task(['RELIANCE.NS', 'HDFC.NS', 'ICICIBANK.NS'])
        self.provider.download.return_value = pd.DataFrame()
        self.assertFalse(task.run())
        self.assertEqual(self.provider.download.call_count, 1)
        self.assertEqual(task.coverage['stop_reason']['symbol'], 'RELIANCE.NS')
        self.assertEqual(task.coverage['pending_provider'], ['ICICIBANK.NS'])
        self.assertEqual(task.coverage['not_returned'], task.symbols)
        self.verify_manifest(task)

    def test_known_archival_does_not_license_ignoring_rate_limit(self):
        errors = [YFRateLimitError('rate limited')]
        for code in (401, 403, 429):
            response = requests.Response(); response.status_code = code
            errors.append(requests.HTTPError('denied', response=response))
        for error in errors:
            with self.subTest(kind=type(error).__name__):
                self.provider.download.reset_mock()
                self.provider.download.side_effect = error
                task = self.task(['HDFC.NS', 'ICICIBANK.NS', 'SBIN.NS'])
                with self.assertRaises(type(error)):
                    task.run()
                self.assertEqual(self.provider.download.call_count, 1)
                self.assertEqual(task.coverage['pending_provider'], ['SBIN.NS'])
                self.assertEqual(task.coverage['stop_reason']['category'], 'source_denied_or_rate_limited')
                self.verify_manifest(task)

    def test_schema_and_unknown_errors_still_stop_after_archival_routing(self):
        for response in (prices().drop(columns='Close'), RuntimeError('unknown')):
            with self.subTest(kind=type(response).__name__):
                self.provider.download.reset_mock()
                self.provider.download.side_effect = [response, prices()]
                task = self.task(['HDFC.NS', 'ICICIBANK.NS', 'SBIN.NS'])
                self.assertFalse(task.run())
                self.assertEqual(self.provider.download.call_count, 1)
                self.assertEqual(task.coverage['pending_provider'], ['SBIN.NS'])

    def test_mixed_frame_and_archive_are_both_still_outstanding(self):
        self.provider.download.side_effect = [mixed(), prices()]
        task = self.task(['RELIANCE.NS', 'HDFC.NS', 'ICICIBANK.NS'])
        self.assertFalse(task.run())
        self.assertEqual(self.provider.download.call_count, 2)
        self.assertEqual(task.coverage['not_returned'], ['RELIANCE.NS', 'HDFC.NS'])
        self.assertEqual(task.coverage['row_quality_rejected'], ['RELIANCE.NS'])
        self.assertEqual(len(task.rejection_evidence), 1)
        self.assertEqual(task.coverage['returned'], ['ICICIBANK.NS'])
        self.verify_manifest(task)

    def test_plan_write_failure_prevents_all_provider_calls(self):
        task = self.task()
        with patch.object(Path, 'write_bytes', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                task.run()
        self.provider.download.assert_not_called()
        self.assertEqual(task.coverage['deferred_archival'], [])
        self.assertEqual(task.coverage['stop_reason']['category'], 'archival_plan_persistence_failed')
        self.assertEqual(list(Path(self.temporary.name).rglob('manifest.json')), [])

    def test_plan_finalization_failure_also_prevents_all_provider_calls(self):
        task = self.task()
        with patch.object(Path, 'replace', side_effect=OSError('rename failed')):
            with self.assertRaises(OSError):
                task.run()
        self.provider.download.assert_not_called()
        self.assertIsNone(task.coverage['archival_plan'])
        self.assertEqual(list(Path(self.temporary.name).rglob('manifest.json')), [])

    def test_reusing_task_preserves_old_manifest_and_clears_current_state(self):
        task = self.task()
        self.assertFalse(task.run())
        first = Path(task.coverage['archival_plan']['path']); original = first.read_bytes()
        self.provider.download.return_value = pd.DataFrame()
        self.assertFalse(task.run())
        self.assertNotEqual(first, Path(task.coverage['archival_plan']['path']))
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(task.coverage['returned'], [])
        self.assertEqual(task.coverage['attempted'], ['ICICIBANK.NS'])

    def test_no_archival_issuer_keeps_normal_success_without_worklist(self):
        task = self.task(['ICICIBANK.NS', 'SBIN.NS'])
        self.assertTrue(task.run())
        self.assertIsNone(task.coverage['archival_plan'])
        self.assertEqual(task.coverage['deferred_archival'], [])
        self.assertEqual(task.coverage['not_returned'], [])
        self.assertEqual(list(Path(self.temporary.name).rglob('manifest.json')), [])

    def test_flag_rejects_truthy_strings_and_integers(self):
        for value in ('false', 'true', 0, 1, None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.task(enabled=value)


if __name__ == '__main__':
    unittest.main()

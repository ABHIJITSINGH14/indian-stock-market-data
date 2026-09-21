"""Offline issuer regression tests. No provider or issuer website is contacted."""
from datetime import date
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from config.issuer_identity import current_snapshot_deferral, reject_known_typo
from test_yahoo_sources import load_adapter, info, YFRateLimitError


class IssuerIdentityTests(unittest.TestCase):
    def adapters(self):
        return (load_adapter('download_nse_data.py', 'NSEDataDownloader'),
                load_adapter('download_fundamentals.py', 'FundamentalsDownloader'))

    def test_only_default_typo_changed_not_history_or_successor(self):
        (prices, _), (fundamentals, _) = self.adapters()
        expected = ['RELIANCE.NS', 'TCS.NS', 'INFY.NS', 'WIPRO.NS', 'HDFC.NS',
                    'ICICIBANK.NS', 'SBIN.NS', 'MARUTI.NS', 'BAJAJFINSV.NS', 'TITAN.NS',
                    'LT.NS', 'NESTLEIND.NS', 'ASIANPAINT.NS', 'SUNPHARMA.NS', 'DRREDDY.NS']
        self.assertEqual(fundamentals.STOCKS, expected)
        self.assertEqual(prices.NSE_STOCKS, expected +
                         ['CIPLA.NS', 'DMART.NS', 'POWERGRID.NS', 'ULTRACEMCO.NS', 'COALINDIA.NS'])
        self.assertNotIn('HDFCBANK.NS', prices.NSE_STOCKS)

    def test_explicit_typo_rejected_without_silent_alias_or_network(self):
        for cls, provider in self.adapters():
            with self.assertRaisesRegex(ValueError, 'request INFY.NS explicitly'):
                cls(symbols=['INFOSY.NS'])
            provider.Ticker.assert_not_called()
            provider.download.assert_not_called()

    def test_only_known_typo_rejected(self):
        for symbol in ('INFY.NS', 'HDFC.NS', 'HDFCBANK.NS', 'INFY.BO', 'UNKNOWN.NS'):
            self.assertIsNone(reject_known_typo(symbol))
        # Not finding an exception is not a full-universe identity certification.
        self.assertIsNone(current_snapshot_deferral('UNKNOWN.NS', date(2026, 9, 21)))

    def test_merger_boundary_is_not_backdated_over_historical_identity(self):
        self.assertIsNone(current_snapshot_deferral('HDFC.NS', date(2023, 6, 30)))
        for day in (date(2023, 7, 1), date(2026, 9, 21)):
            item = current_snapshot_deferral('HDFC.NS', day)
            self.assertEqual(item['status'], 'archival_required')
            self.assertEqual(item['effective_date'], '2023-07-01')
            self.assertTrue(item['historical_obligation_preserved'])
            self.assertIsNone(item['replacement_symbol'])
            self.assertTrue(item['evidence_url'].startswith('https://www.hdfc.bank.in/'))
        self.assertIsNone(current_snapshot_deferral('HDFCBANK.NS', date(2026, 9, 21)))

    def test_deferral_records_are_fresh_not_shared_mutable_state(self):
        first = current_snapshot_deferral('HDFC.NS', date(2026, 9, 21))
        first['replacement_symbol'] = 'MUST_NOT_LEAK.NS'
        self.assertIsNone(current_snapshot_deferral('HDFC.NS', date(2026, 9, 21))['replacement_symbol'])

    def test_retired_only_makes_no_call_and_does_not_report_success(self):
        cls, provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        with tempfile.TemporaryDirectory() as temporary:
            task = cls(['HDFC.NS'], Path(temporary) / 'fundamentals.csv')
            self.assertFalse(task.run())
            provider.Ticker.assert_not_called()
            self.assertTrue(task.data.empty)
            self.assertFalse(task.output_path.exists())
            self.assertEqual(task.coverage['not_returned'], ['HDFC.NS'])
            self.assertEqual(task.coverage['returned'], [])
            self.assertEqual(set(task.coverage['deferred']), {'HDFC.NS'})

    def test_retired_does_not_block_later_issuers_or_shrink_denominator(self):
        cls, provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        provider.Ticker.side_effect = lambda symbol: types.SimpleNamespace(info=info(symbol))
        with tempfile.TemporaryDirectory() as temporary, patch('time.sleep') as sleep:
            symbols = ['INFY.NS', 'HDFC.NS', 'TCS.NS']
            task = cls(symbols, Path(temporary) / 'fundamentals.csv')
            self.assertFalse(task.run())
            self.assertEqual([c.args[0] for c in provider.Ticker.call_args_list], ['INFY.NS', 'TCS.NS'])
            self.assertEqual(task.coverage['requested'], symbols)
            self.assertEqual(task.coverage['not_returned'], ['HDFC.NS'])
            self.assertEqual(task.coverage['returned'], ['INFY.NS', 'TCS.NS'])
            self.assertEqual(list(task.data['provider_symbol']), ['INFY.NS', 'TCS.NS'])
            self.assertTrue(task.output_path.exists())
            sleep.assert_called_once_with(2)

    def test_explicit_corrected_identity_can_succeed(self):
        cls, provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        provider.Ticker.return_value = types.SimpleNamespace(info=info('INFY.NS'))
        with tempfile.TemporaryDirectory() as temporary:
            task = cls(['INFY.NS'], Path(temporary) / 'fundamentals.csv')
            self.assertTrue(task.run())
            self.assertEqual(task.data.iloc[0]['symbol'], 'INFY')
            self.assertEqual(task.coverage['deferred'], {})
            self.assertEqual(task.coverage['not_returned'], [])

    def test_direct_retired_fetch_is_also_guarded(self):
        cls, provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        task = cls(['HDFC.NS'])
        self.assertIsNone(task.fetch_stock_info('HDFC.NS'))
        provider.Ticker.assert_not_called()
        self.assertEqual(task.last_response_metadata['status'], 'archival_required')

    def test_provider_denial_still_stops_after_known_deferral(self):
        cls, provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        provider.Ticker.side_effect = YFRateLimitError()
        task = cls(['HDFC.NS', 'INFY.NS', 'TCS.NS'])
        with self.assertRaises(YFRateLimitError):
            task.download_all_fundamentals()
        self.assertEqual(provider.Ticker.call_count, 1)
        self.assertEqual(task.coverage['not_returned'], task.symbols)
        self.assertEqual(set(task.coverage['deferred']), {'HDFC.NS'})

    def test_missing_cap_stays_failure_after_identity_correction(self):
        cls, provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        payload = info('INFY.NS')
        payload.pop('marketCap')
        provider.Ticker.return_value = types.SimpleNamespace(info=payload)
        task = cls(['INFY.NS', 'TCS.NS'])
        self.assertFalse(task.download_all_fundamentals())
        self.assertEqual(provider.Ticker.call_count, 1)
        self.assertEqual(task.coverage['returned'], [])

    def test_reuse_clears_prior_snapshot_rows_and_deferrals(self):
        cls, provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        provider.Ticker.return_value = types.SimpleNamespace(info=info('INFY.NS'))
        task = cls(['INFY.NS', 'HDFC.NS'])
        self.assertFalse(task.download_all_fundamentals())
        task.symbols = ['INFY.NS']
        self.assertTrue(task.download_all_fundamentals())
        self.assertEqual(task.coverage['deferred'], {})
        self.assertEqual(len(task.data), 1)

    def test_exception_diagnostics_do_not_reuse_previous_issuer_metadata(self):
        cls, provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        task = cls(['INFY.NS'])
        task.fetch_stock_info('HDFC.NS')
        provider.Ticker.side_effect = YFRateLimitError()
        with self.assertRaises(YFRateLimitError):
            task.fetch_stock_info('INFY.NS')
        self.assertEqual(task.last_response_metadata, {})


if __name__ == '__main__':
    unittest.main()

"""Deterministic Yahoo adapter contracts; all provider calls are replaced with doubles."""
import copy
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

ROOT = Path(__file__).resolve().parents[1]


class YFRateLimitError(Exception):
    pass


def load_adapter(filename, class_name):
    provider = types.SimpleNamespace(download=Mock(), Ticker=Mock(), __version__='1.7.0')
    with patch.dict(sys.modules, {'yfinance': provider,
                                 'yfinance.exceptions': types.SimpleNamespace(YFRateLimitError=YFRateLimitError)}):
        namespace = runpy.run_path(str(ROOT / 'scripts' / filename), run_name='yahoo_contract_' + class_name)
    return namespace[class_name], provider


def prices():
    return pd.DataFrame({'Open': [10., 11.], 'High': [12., 13.], 'Low': [9., 10.],
                         'Close': [11., 12.], 'Adj Close': [5.5, 6.], 'Volume': [100, 200]},
                        index=pd.to_datetime(['2026-09-14', '2026-09-15']))


def info(symbol='RELIANCE.NS'):
    return {'symbol': symbol, 'longName': 'Fixture issuer', 'marketCap': 1000000,
            'currency': 'INR', 'financialCurrency': 'INR', 'trailingPE': 20.0}


class YahooPriceTests(unittest.TestCase):
    def setUp(self):
        self.cls, self.provider = load_adapter('download_nse_data.py', 'NSEDataDownloader')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.task = self.cls(symbols=['RELIANCE.NS'], start_date='2026-09-14', end_date='2026-09-19',
                             output_path=Path(self.temp.name, 'prices.csv'))
        self.provider.download.return_value = prices()
        self.sleep = patch('time.sleep').start()
        self.addCleanup(patch.stopall)

    def test_success_keeps_adjusted_close_separate_and_explicit_semantics(self):
        self.assertTrue(self.task.run())
        frame = pd.read_csv(self.task.output_path)
        self.assertEqual(list(frame['close']), [11., 12.])
        self.assertEqual(list(frame['adj_close']), [5.5, 6.])
        self.assertEqual(set(frame['source']), {'yahoo_finance'})
        self.assertEqual(set(frame['provider_symbol']), {'RELIANCE.NS'})
        self.assertEqual(set(frame['provider_version']), {'1.7.0'})
        self.assertTrue(frame['retrieved_at_utc'].str.endswith('+00:00').all())
        kw = self.provider.download.call_args.kwargs
        for k in ('auto_adjust', 'back_adjust', 'repair', 'rounding', 'threads', 'multi_level_index'):
            self.assertIs(kw[k], False)
        self.assertIs(kw['keepna'], True)
        self.assertEqual(kw['interval'], '1d')
        self.assertEqual(kw['end'], date(2026, 9, 19))
        self.assertEqual(self.task.coverage['historical_completeness'], 'not_verified')

    def test_empty_and_none_fail_without_file(self):
        for data in (None, pd.DataFrame()):
            self.provider.download.return_value = data
            self.assertFalse(self.task.run())
            self.assertFalse(self.task.output_path.exists())

    def test_ambiguous_or_incomplete_column_shapes_are_rejected(self):
        frames = [prices().drop(columns='Close')]
        multi = prices()
        multi.columns = pd.MultiIndex.from_product([multi.columns, ['RELIANCE.NS']])
        frames.append(multi)
        duplicate = prices()
        duplicate.columns = ['Close'] * len(duplicate.columns)
        frames.append(duplicate)
        for frame in frames:
            self.provider.download.return_value = frame
            self.assertFalse(self.task.run())

    def test_invalid_close_values_are_rejected_not_filled(self):
        for value in (None, float('nan'), float('inf'), float('-inf'), 0, -1, 'not-a-number'):
            frame = prices().astype(object)
            frame.iloc[0, frame.columns.get_loc('Close')] = value
            self.provider.download.return_value = frame
            self.assertFalse(self.task.run())

    def test_bad_ohlc_ordering_is_rejected(self):
        frame = prices()
        frame['High'] = [1., 1.]
        self.provider.download.return_value = frame
        self.assertFalse(self.task.run())

    def test_negative_or_missing_volume_rejected(self):
        for value in (-1, float('nan'), float('inf')):
            frame = prices()
            frame['Volume'] = value
            self.provider.download.return_value = frame
            self.assertFalse(self.task.run())

    def test_dates_outside_bounds_or_duplicates_are_rejected(self):
        for index in (pd.to_datetime(['2026-09-13', '2026-09-15']),
                      pd.to_datetime(['2026-09-14', '2026-09-19']),
                      pd.to_datetime(['2026-09-14', '2026-09-14']),
                      pd.to_datetime(['2026-09-14', None]),
                      pd.RangeIndex(2)):
            frame = prices()
            frame.index = index
            self.provider.download.return_value = frame
            self.assertFalse(self.task.run())

    def test_sorting_is_chronological_without_mutating_provider_frame(self):
        frame = prices().iloc[::-1]
        original = frame.copy(deep=True)
        self.provider.download.return_value = frame
        result = self.task.download_stock_data('RELIANCE.NS')
        self.assertEqual(list(result['Date']), ['2026-09-14', '2026-09-15'])
        pd.testing.assert_frame_equal(original, frame)

    def test_partial_result_saved_but_not_success_and_no_more_tickers(self):
        self.task.symbols = ['RELIANCE.NS', 'TCS.NS', 'WIPRO.NS']
        self.provider.download.side_effect = [prices(), pd.DataFrame()]
        self.assertFalse(self.task.run())
        self.assertEqual(self.provider.download.call_count, 2)
        self.assertTrue(self.task.output_path.exists())
        self.assertEqual(self.task.coverage['not_returned'], ['TCS.NS', 'WIPRO.NS'])
        self.assertEqual(self.task.coverage['returned'], ['RELIANCE.NS'])

    def test_library_rate_limit_stops_without_retry_and_preserves_prior_observations(self):
        self.task.symbols = ['RELIANCE.NS', 'TCS.NS', 'WIPRO.NS']
        self.provider.download.side_effect = [prices(), YFRateLimitError()]
        with self.assertRaises(YFRateLimitError):
            self.task.run()
        self.assertEqual(self.provider.download.call_count, 2)
        self.assertTrue(self.task.output_path.exists())

    def test_explicit_http_denial_propagates(self):
        for code in (401, 403, 429):
            response = requests.Response()
            response.status_code = code
            self.provider.download.side_effect = requests.HTTPError(response=response)
            with self.assertRaises(requests.HTTPError):
                self.task.run()

    def test_fractional_volume_and_invalid_adjusted_close_rejected(self):
        for column, value in [('Volume', 1.5), ('Adj Close', float('inf')), ('Adj Close', 0), ('Close', True)]:
            frame = prices()
            frame[column] = value
            self.provider.download.return_value = frame
            self.assertFalse(self.task.run())

    def test_optional_adjusted_close_gaps_are_not_filled(self):
        frame = prices()
        frame['Adj Close'] = [float('nan'), 6.]
        self.provider.download.return_value = frame
        self.assertTrue(self.task.run())
        self.assertTrue(pd.isna(pd.read_csv(self.task.output_path)['adj_close'].iloc[0]))

    def test_write_failure_is_not_success(self):
        with patch.object(self.task.processor, 'save_csv', return_value=False):
            self.assertFalse(self.task.run())

    def test_reused_collector_does_not_append_stale_frames(self):
        self.assertTrue(self.task.download_all_stocks())
        self.provider.download.return_value = None
        self.assertFalse(self.task.download_all_stocks())
        self.assertTrue(self.task.all_data.empty)
        self.assertEqual(self.task.coverage['returned'], [])

    def test_invalid_configuration_rejected(self):
        for symbols in ([], ['RELIANCE.NS', 'RELIANCE.NS'], ['bad symbol'], 'RELIANCE.NS'):
            with self.assertRaises(ValueError):
                self.cls(symbols=symbols)
        for start, end in (('2026-09-20', '2026-09-19'), ('2026-09-19', '2026-09-19')):
            with self.assertRaises(ValueError):
                self.cls(start_date=start, end_date=end)


class YahooFundamentalsTests(unittest.TestCase):
    def setUp(self):
        self.cls, self.provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.task = self.cls(symbols=['RELIANCE.NS'], output_path=Path(self.temp.name, 'fundamentals.csv'))
        self.provider.Ticker.return_value = types.SimpleNamespace(info=info())
        patch('time.sleep').start()
        self.addCleanup(patch.stopall)

    def test_snapshot_source_currency_and_missing_values_preserved(self):
        self.assertTrue(self.task.run())
        frame = pd.read_csv(self.task.output_path)
        self.assertEqual(frame['market_cap'].iloc[0], 1000000)
        self.assertEqual(frame['source'].iloc[0], 'yahoo_finance')
        self.assertEqual(frame['provider_symbol'].iloc[0], 'RELIANCE.NS')
        self.assertEqual(frame['market_cap_currency'].iloc[0], 'INR')
        self.assertTrue(frame['snapshot_only'].iloc[0])
        self.assertTrue(pd.isna(frame['book_value'].iloc[0]))
        self.assertEqual(self.task.coverage['historical_completeness'], 'not_verified')

    def test_missing_bad_or_boolean_cap_rejected(self):
        for cap in (None, 'N/A', 0, -10, float('nan'), float('inf'), True):
            payload = info()
            payload['marketCap'] = cap
            self.provider.Ticker.return_value = types.SimpleNamespace(info=payload)
            self.assertFalse(self.task.run())
            self.assertFalse(self.task.output_path.exists())

    def test_wrong_symbol_currency_or_payload_rejected(self):
        for payload in ({}, None, [], info('TCS.NS'), {**info(), 'currency': 'USD'}, {**info(), 'currency': None}):
            self.provider.Ticker.return_value = types.SimpleNamespace(info=payload)
            self.assertFalse(self.task.run())

    def test_rate_limit_exception_from_new_library_stops_immediately(self):
        self.task.symbols = ['RELIANCE.NS', 'TCS.NS']
        self.provider.Ticker.side_effect = YFRateLimitError()
        with self.assertRaises(YFRateLimitError):
            self.task.run()
        self.assertEqual(self.provider.Ticker.call_count, 1)

    def test_generic_http_exception_with_denial_response_stops(self):
        for code in (401, 403, 429):
            exc = RuntimeError('transport denial')
            exc.response = types.SimpleNamespace(status_code=code)
            self.provider.Ticker.side_effect = exc
            with self.assertRaises(RuntimeError):
                self.task.run()

    def test_partial_fundamentals_saved_without_false_success(self):
        self.task.symbols = ['RELIANCE.NS', 'TCS.NS', 'WIPRO.NS']
        self.provider.Ticker.side_effect = [types.SimpleNamespace(info=info()), types.SimpleNamespace(info={})]
        self.assertFalse(self.task.run())
        self.assertTrue(self.task.output_path.exists())
        self.assertEqual(self.provider.Ticker.call_count, 2)
        self.assertEqual(self.task.coverage['not_returned'], ['TCS.NS', 'WIPRO.NS'])

    def test_write_failure_and_repeated_calls(self):
        with patch.object(self.task.processor, 'save_csv', return_value=False):
            self.assertFalse(self.task.run())
        self.provider.Ticker.return_value = types.SimpleNamespace(info={})
        self.assertFalse(self.task.download_all_fundamentals())
        self.assertTrue(self.task.data.empty)


if __name__ == '__main__':
    unittest.main()

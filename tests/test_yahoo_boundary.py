"""Offline response-boundary tests; fixtures are synthetic, never market observations."""
import copy
from datetime import date
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
from scripts.audit_yahoo_boundary import (
    ChartObserver, FIELDS, MAX_BYTES, chart_request, compare_frame, decode_chart,
    request_scope, write_json,
)

SCOPE = request_scope('RELIANCE.NS', date(2026, 9, 18), date(2026, 9, 22))
URL = 'https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS'
PARAMS = {key: SCOPE[key] for key in ('period1', 'period2', 'interval')}


def payload():
    return {'chart': {'error': None, 'result': [{
        'meta': {'symbol': 'RELIANCE.NS', 'currency': 'INR', 'instrumentType': 'EQUITY',
                 'exchangeTimezoneName': 'Asia/Kolkata', 'dataGranularity': '1d'},
        'timestamp': [SCOPE['period1'] + 33300, SCOPE['period1'] + 3 * 86400 + 33300],
        'indicators': {'quote': [{'open': [10., 12.], 'high': [12., 14.],
                                  'low': [9., 11.], 'close': [11., None],
                                  'volume': [100, 200]}],
                       'adjclose': [{'adjclose': [11., None]}]},
    }]}}


def frame():
    return pd.DataFrame({'Open': [10., 12.], 'High': [12., 14.], 'Low': [9., 11.],
        'Close': [11., float('nan')], 'Adj Close': [11., float('nan')], 'Volume': [100, 200]},
        index=pd.to_datetime(['2026-09-18', '2026-09-21']))


def decode(obj=None):
    return decode_chart(json.dumps(payload() if obj is None else obj).encode(), SCOPE)


class BoundaryTests(unittest.TestCase):
    def test_scope_is_bounded_before_network(self):
        for symbol, start, end in [('bad/input', '2026-09-18', '2026-09-22'),
                                  ('RELIANCE.NS', '2026-09-18', '2026-09-18'),
                                  ('RELIANCE.NS', '2026-09-01', '2026-09-22')]:
            with self.subTest(symbol=symbol, start=start), self.assertRaises(ValueError):
                request_scope(symbol, date.fromisoformat(start), date.fromisoformat(end))

    def test_raw_null_is_not_fabricated(self):
        self.assertIsNone(decode()['2026-09-21']['close'])

    def test_identity_currency_and_granularity_checked(self):
        for key, value in [('symbol', 'TCS.NS'), ('currency', 'USD'),
                           ('exchangeTimezoneName', 'UTC'), ('instrumentType', 'ETF'),
                           ('dataGranularity', '1wk')]:
            obj = payload(); obj['chart']['result'][0]['meta'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): decode(obj)

    def test_ambiguous_result_rejected(self):
        obj = payload(); obj['chart']['result'] *= 2
        with self.assertRaises(ValueError): decode(obj)

    def test_chart_error_rejected(self):
        obj = payload(); obj['chart']['error'] = {'code': 'Not Found'}
        with self.assertRaises(ValueError): decode(obj)

    def test_array_lengths_checked(self):
        obj = payload(); obj['chart']['result'][0]['indicators']['quote'][0]['close'].pop()
        with self.assertRaises(ValueError): decode(obj)

    def test_raw_duplicate_and_outside_dates_rejected(self):
        for times in [[SCOPE['period1']] * 2,
                      [SCOPE['period1'], SCOPE['period2']],
                      [True, SCOPE['period2'] - 1]]:
            obj = payload(); obj['chart']['result'][0]['timestamp'] = times
            with self.subTest(times=times), self.assertRaises(ValueError): decode(obj)

    def test_strings_bools_and_nonstandard_numbers_rejected(self):
        for v in ['12', True, float('nan'), float('inf')]:
            obj = payload(); obj['chart']['result'][0]['indicators']['quote'][0]['close'][1] = v
            with self.subTest(value=v), self.assertRaises(ValueError): decode(obj)

    def test_oversize_and_html_rejected(self):
        for raw in [b'x' * (MAX_BYTES + 1), b'<html>error</html>']:
            with self.assertRaises(ValueError): decode_chart(raw, SCOPE)

    def test_missing_upstream_distinguished_from_adapter_loss(self):
        raw = decode(); original = frame(); before = original.copy(deep=True)
        result = compare_frame(raw, original)
        self.assertEqual(sum(r['classification'] == 'missing_in_source_and_frame' for r in result), 2)
        raw['2026-09-21']['close'] = 13.
        result = compare_frame(raw, original)
        self.assertIn('adapter_only_missing', [r['classification'] for r in result])
        pd.testing.assert_frame_equal(original, before)

    def test_changes_and_imputation_not_hidden(self):
        original = frame(); original.iloc[1, original.columns.get_loc('Close')] = 12.
        original.iloc[0, original.columns.get_loc('Open')] = 10.1
        kinds = [r['classification'] for r in compare_frame(decode(), original)]
        self.assertIn('source_missing_adapter_changed', kinds)
        self.assertIn('value_changed', kinds)

    def test_missing_dates_not_hidden(self):
        original = frame(); original.index = pd.to_datetime(['2026-09-18', '2026-09-20'])
        kinds = [r['classification'] for r in compare_frame(decode(), original)]
        self.assertIn('source_only_date', kinds); self.assertIn('adapter_only_date', kinds)

    def test_ambiguous_adapter_frames_rejected(self):
        duplicate = frame(); duplicate.index = pd.to_datetime(['2026-09-18'] * 2)
        for value in [None, frame().drop(columns=['Close']), duplicate]:
            with self.assertRaises(ValueError): compare_frame(decode(), value)

    def test_request_matching_ignores_timezone_probe(self):
        self.assertTrue(chart_request(URL, PARAMS, SCOPE))
        for url, params in [(URL, {'range': '1d'}),
                            (URL.replace('query1.finance.yahoo.com', 'example.invalid'), PARAMS),
                            (URL.replace('RELIANCE', 'TCS'), PARAMS)]:
            self.assertFalse(chart_request(url, params, SCOPE))

    def test_observer_returns_identical_response_and_forwards_args_once(self):
        body = json.dumps(payload()).encode()
        response = SimpleNamespace(status_code=200, content=body)
        original = Mock(return_value=response); instance = object()
        observer = ChartObserver(original, SCOPE)
        kwargs = {'url': URL, 'params': copy.deepcopy(PARAMS), 'timeout': 30}
        before = copy.deepcopy(kwargs)
        self.assertIs(observer.get(instance, **kwargs), response)
        original.assert_called_once_with(instance, **kwargs)
        self.assertEqual(kwargs, before); self.assertEqual(observer.responses[0]['body'], body)

    def test_denied_and_wrong_identity_bodies_are_not_saved(self):
        wrong = payload(); wrong['chart']['result'][0]['meta']['symbol'] = 'OTHER.NS'
        for status, content in [(403, b'secret cookie'), (429, b'secret crumb'),
                                 (200, json.dumps(wrong).encode())]:
            observer = ChartObserver(Mock(return_value=SimpleNamespace(status_code=status, content=content)), SCOPE)
            observer.get(object(), url=URL, params=PARAMS)
            self.assertNotIn('body', observer.responses[0])
            self.assertNotIn('secret', json.dumps(observer.responses))

    def test_observer_preserves_original_exception(self):
        original = Mock(side_effect=TimeoutError('timeout'))
        observer = ChartObserver(original, SCOPE)
        with self.assertRaises(TimeoutError): observer.get(object(), url=URL, params=PARAMS)
        self.assertEqual(observer.responses, [])

    def test_atomic_json_disallows_nan(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'manifest.json'
            write_json(p, {'value': None})
            self.assertEqual(json.loads(p.read_text()), {'value': None})
            self.assertFalse(p.with_suffix('.json.tmp').exists())
            with self.assertRaises(ValueError): write_json(p, {'value': float('nan')})
            self.assertEqual(json.loads(p.read_text()), {'value': None})

"""Synthetic regression cases shaped by October's failures; no network calls."""
import copy
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import requests

from scripts.download_bse_data import BSEDataDownloader
from scripts.corporate_actions import CorporateActionsDownloader
from test_collector_contracts import response
from test_yahoo_sources import load_adapter, info, prices, YFRateLimitError
from utils.api_client import APIClient
from utils.price_evidence import audit_price_frame


def company():
    return {'Scrip Code': '500001', 'Scrip Name': 'SYNTHETIC FIXTURE ONLY',
            'ISIN': 'INE000A01010', 'Group': 'A', 'Status': 'Active'}


class BSEFailureTests(unittest.TestCase):
    def setUp(self):
        self.task = BSEDataDownloader()
        self.addCleanup(self.task.client.close)

    def fetch(self, payload):
        with patch.object(self.task.client, 'get', return_value=response(text=json.dumps(payload))):
            return self.task.fetch_bse_equities_list()

    def test_valid_table_keeps_identity_values(self):
        self.assertTrue(self.fetch({'Table': [company()]}))
        self.assertEqual(self.task.data.iloc[0]['code'], '500001')
        self.assertEqual(self.task.coverage['admitted_rows'], 1)
        self.assertEqual(self.task.coverage['historical_completeness'], 'not_verified')

    def test_wrong_json_shapes_are_never_success(self):
        for payload in ({}, [], None, {'Table': []}, {'Table': {}}, {'Table': [None]}):
            with self.subTest(payload=payload):
                self.assertFalse(self.fetch(payload))
                self.assertTrue(self.task.data.empty)
                self.assertEqual(self.task.coverage['failure_stage'], 'payload_validation')

    def test_non_json_is_distinct_from_schema_error(self):
        with patch.object(self.task.client, 'get', return_value=response(text='<html>Unavailable</html>')):
            self.assertFalse(self.task.fetch_bse_equities_list())
        self.assertEqual(self.task.coverage['failure_stage'], 'payload_decode')
        self.assertEqual(self.task.coverage['admitted_rows'], 0)

    def test_missing_and_ambiguous_identities_reject_entire_table(self):
        cases = []
        for field, value in [('Scrip Code', True), ('Scrip Code', 500001.0),
                             ('Scrip Name', ''), ('ISIN', None), ('ISIN', 'BAD')]:
            row = company(); row[field] = value; cases.append([row])
        cases.append([company(), company()])
        row = company(); row['Scrip Code'] = '500002'; cases.append([company(), row])
        row = company(); row['Scrip Code'] = '500002'; row.pop('ISIN'); cases.append([company(), row])
        for rows in cases:
            with self.subTest(rows=rows):
                self.assertFalse(self.fetch({'Table': rows}))
                self.assertTrue(self.task.data.empty)

    def test_reused_collector_does_not_save_stale_success(self):
        self.assertTrue(self.fetch({'Table': [company()]}))
        with patch.object(self.task.client, 'get', side_effect=requests.exceptions.SSLError('TLS')), \
             patch.object(self.task.processor, 'save_csv') as save:
            self.assertFalse(self.task.run())
            save.assert_not_called()
        self.assertTrue(self.task.data.empty)
        self.assertEqual(self.task.coverage['failure_stage'], 'source_request')


class SourceMetadataTests(unittest.TestCase):
    def test_response_fingerprint_without_body_query_or_cookies(self):
        client = APIClient(); self.addCleanup(client.close)
        value = response(text='<html>secret error body</html>')
        value.headers['Content-Type'] = 'text/html; charset=UTF-8'
        value.headers['Set-Cookie'] = 'secret'
        with patch.object(client.session, 'get', return_value=value), patch('time.sleep'):
            self.assertIs(client.get('https://example.invalid/data?token=SECRET'), value)
        m = client.last_request_metadata
        self.assertEqual(m['body_sha256'], hashlib.sha256(value.content).hexdigest())
        self.assertEqual(m['content_type'], 'text/html')
        self.assertEqual(m['source_url'], 'https://example.invalid/data')
        self.assertNotIn('secret', json.dumps(m).lower())

    def test_tls_failure_remains_one_attempt_and_no_previous_response(self):
        client = APIClient(); self.addCleanup(client.close)
        with patch.object(client.session, 'get', side_effect=[response(), requests.exceptions.SSLError('SECRET')]) as get, \
             patch('time.sleep'):
            client.get('https://example.invalid/data')
            with self.assertRaises(requests.exceptions.SSLError): client.get('https://example.invalid/data')
        m = client.last_request_metadata
        self.assertEqual(get.call_count, 2)
        self.assertEqual(m['attempts'], 1)
        self.assertEqual(m['failure_category'], 'tls_error')
        self.assertNotIn('body_sha256', m)
        self.assertNotIn('SECRET', json.dumps(m))

    def test_corporate_tls_failure_cannot_save_a_dataset(self):
        task = CorporateActionsDownloader(); self.addCleanup(task.client.close)
        with patch.object(task.client.session, 'get', side_effect=requests.exceptions.SSLError('TLS')) as get, \
             patch('time.sleep'), patch.object(task.processor, 'save_csv') as save:
            self.assertFalse(task.run())
            save.assert_not_called(); self.assertEqual(get.call_count, 1)
        self.assertEqual(task.client.last_request_metadata['failure_category'], 'tls_error')


class FundamentalsEvidenceTests(unittest.TestCase):
    def test_missing_cap_shape_and_stop_are_preserved_without_fallback(self):
        cls, provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        payload = info(); payload.pop('marketCap'); payload['nonDilutedMarketCap'] = 1000000
        provider.Ticker.return_value.info = payload
        before = copy.deepcopy(payload)
        task = cls(['RELIANCE.NS', 'TCS.NS'])
        self.assertFalse(task.download_all_fundamentals())
        self.assertTrue(task.data.empty); self.assertEqual(provider.Ticker.call_count, 1)
        m = task.coverage['observations']['RELIANCE.NS']
        self.assertFalse(m['market_cap_present']); self.assertEqual(m['market_cap_type'], 'NoneType')
        self.assertTrue(m['symbol_matches']); self.assertEqual(m['currency'], 'INR')
        self.assertEqual(task.coverage['stop_reason']['category'], 'market_cap_unverified')
        self.assertEqual(task.coverage['not_attempted'], ['TCS.NS'])
        self.assertEqual(payload, before)

    def test_denial_is_not_reclassified_as_missing_market_cap(self):
        cls, provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        provider.Ticker.side_effect = YFRateLimitError()
        task = cls(['RELIANCE.NS', 'TCS.NS'])
        with self.assertRaises(YFRateLimitError): task.download_all_fundamentals()
        self.assertEqual(task.coverage['observations']['RELIANCE.NS'], {})
        self.assertEqual(task.coverage['stop_reason']['error_type'], 'YFRateLimitError')


class PriceIntegrityTests(unittest.TestCase):
    def test_weekend_dates_are_not_filtered_and_nan_prices_are_not_filled(self):
        frame = prices()
        frame.index = pd.to_datetime(['2010-02-05', '2010-02-06'])
        before = frame.copy(deep=True)
        self.assertTrue(audit_price_frame(frame, date(2010, 2, 5), date(2010, 2, 9)).passed)
        for field in ('Open', 'High', 'Low', 'Close'): frame.loc['2010-02-06', field] = float('nan')
        bad = frame.copy(deep=True)
        audit = audit_price_frame(frame, date(2010, 2, 5), date(2010, 2, 9))
        self.assertFalse(audit.passed)
        self.assertEqual(audit.rejected[0]['reasons'], ['missing:' + f for f in ('Open', 'High', 'Low', 'Close')])
        pd.testing.assert_frame_equal(frame, bad)
        self.assertEqual(before.loc['2010-02-06', 'Close'], 12.)


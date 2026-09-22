"""A numerical data-quality defect is not permission to ignore source failures."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import requests
from test_yahoo_sources import load_adapter, prices, YFRateLimitError


def mixed():
    frame = prices()
    frame.loc[frame.index[0], ['Open', 'High', 'Low', 'Close', 'Adj Close']] = float('nan')
    frame.loc[frame.index[0], 'Volume'] = 0
    return frame


class PriceContinuationTests(unittest.TestCase):
    def setUp(self):
        self.cls, self.provider = load_adapter('download_nse_data.py', 'NSEDataDownloader')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sleep = patch('time.sleep').start()
        self.addCleanup(patch.stopall)

    def task(self, symbols=None, enabled=True):
        return self.cls(symbols=symbols or ['RELIANCE.NS', 'TCS.NS'],
                        start_date='2026-09-14', end_date='2026-09-19',
                        output_path=Path(self.temp.name, 'prices.csv'),
                        continue_after_row_rejection=enabled)

    def test_mixed_then_valid_examines_next_but_original_task_fails(self):
        first = mixed()
        before = first.copy(deep=True)
        self.provider.download.side_effect = [first, prices()]
        task = self.task()
        self.assertFalse(task.run())
        self.assertEqual(self.provider.download.call_count, 2)
        self.assertEqual(task.coverage['attempted'], ['RELIANCE.NS', 'TCS.NS'])
        self.assertEqual(task.coverage['not_attempted'], [])
        self.assertEqual(task.coverage['returned'], ['TCS.NS'])
        self.assertEqual(task.coverage['not_returned'], ['RELIANCE.NS'])
        self.assertEqual(task.coverage['row_quality_rejected'], ['RELIANCE.NS'])
        self.assertIsNone(task.coverage['stop_reason'])
        self.assertEqual(set(pd.read_csv(task.output_path)['provider_symbol']), {'TCS.NS'})
        pd.testing.assert_frame_equal(before, first)
        record = task.coverage['observations']['RELIANCE.NS']
        self.assertEqual(record['frame_rows'], record['usable_rows'] + record['rejected_rows'])
        manifest = json.loads(Path(record['manifest']).read_text())
        self.assertFalse(manifest['collector_admission'])
        self.assertEqual(task.coverage['historical_completeness'], 'not_verified')
        self.assertTrue(self.provider.download.call_args.kwargs['keepna'])
        self.sleep.assert_called_once_with(2)

    def test_two_mixed_frames_remain_failed_with_no_production_file(self):
        self.provider.download.side_effect = [mixed(), mixed()]
        task = self.task()
        self.assertFalse(task.run())
        self.assertEqual(self.provider.download.call_count, 2)
        self.assertEqual(len(task.rejection_evidence), 2)
        self.assertEqual(task.coverage['not_returned'], task.symbols)
        self.assertEqual(task.coverage['returned'], [])
        self.assertFalse(task.output_path.exists())
        self.assertEqual(set(task.coverage['observations']), set(task.symbols))

    def test_all_valid_still_succeeds(self):
        self.provider.download.side_effect = [prices(), prices()]
        task = self.task()
        self.assertTrue(task.run())
        self.assertEqual(task.coverage['returned'], task.symbols)
        self.assertEqual(task.coverage['not_returned'], [])
        self.assertEqual(task.coverage['row_quality_rejected'], [])
        self.assertEqual(len(pd.read_csv(task.output_path)), 4)

    def test_opt_out_keeps_prior_strict_stop_behavior(self):
        self.provider.download.side_effect = [mixed(), prices()]
        task = self.task(enabled=False)
        self.assertFalse(task.run())
        self.assertEqual(self.provider.download.call_count, 1)
        self.assertEqual(task.coverage['not_attempted'], ['TCS.NS'])
        self.assertEqual(task.coverage['stop_reason']['category'], 'row_quality_rejected')

    def assert_stops(self, frame):
        self.provider.download.reset_mock()
        self.provider.download.side_effect = [frame, prices()]
        task = self.task()
        self.assertFalse(task.run())
        self.assertEqual(self.provider.download.call_count, 1)
        self.assertEqual(task.coverage['not_attempted'], ['TCS.NS'])
        self.assertEqual(task.coverage['row_quality_rejected'], [])
        self.assertIsNotNone(task.coverage['stop_reason'])

    def test_empty_or_none_is_not_a_safe_row_rejection(self):
        for frame in (None, pd.DataFrame()):
            with self.subTest(frame=type(frame).__name__):
                self.assert_stops(frame)

    def test_all_missing_frame_stops(self):
        frame = prices()
        frame[['Open', 'High', 'Low', 'Close']] = float('nan')
        self.assert_stops(frame)

    def test_schema_fault_stops(self):
        self.assert_stops(prices().drop(columns='Close'))

    def test_ambiguous_columns_stop(self):
        frame = prices()
        frame.columns = ['X'] * len(frame.columns)
        self.assert_stops(frame)

    def test_nonnumeric_dtype_stops(self):
        self.assert_stops(prices().astype(object))

    def test_duplicate_date_is_not_a_safe_value_gap(self):
        frame = pd.concat([prices(), prices().iloc[[0]]])
        self.assert_stops(frame)

    def test_out_of_range_date_stops_even_with_usable_rows(self):
        frame = prices()
        frame.index = pd.to_datetime(['2026-09-14', '2026-09-19'])
        self.assert_stops(frame)

    def test_missing_date_stops_even_with_usable_rows(self):
        frame = prices()
        frame.index = pd.to_datetime(['2026-09-14', None])
        self.assert_stops(frame)

    def test_evidence_write_failure_stops(self):
        self.provider.download.side_effect = [mixed(), prices()]
        task = self.task()
        with patch.object(Path, 'write_bytes', side_effect=OSError('disk full')):
            self.assertFalse(task.run())
        self.assertEqual(self.provider.download.call_count, 1)
        self.assertEqual(task.coverage['stop_reason']['category'], 'source_or_processing_error')
        self.assertEqual(task.coverage['row_quality_rejected'], [])
        self.assertEqual(list(task.evidence_dir.rglob('manifest.json')), [])

    def test_explicit_denials_never_continue_or_retry(self):
        errors = [YFRateLimitError('limited')]
        for status in (401, 403, 429):
            response = requests.Response()
            response.status_code = status
            errors.append(requests.HTTPError('denied', response=response))
        for error in errors:
            with self.subTest(kind=type(error).__name__, message=str(error)):
                self.provider.download.reset_mock()
                self.provider.download.side_effect = [mixed(), error, prices()]
                task = self.task(symbols=['RELIANCE.NS', 'TCS.NS', 'INFY.NS'])
                with self.assertRaises(type(error)):
                    task.run()
                self.assertEqual(self.provider.download.call_count, 2)
                self.assertEqual(task.coverage['not_attempted'], ['INFY.NS'])
                self.assertEqual(task.coverage['stop_reason']['symbol'], 'TCS.NS')
                self.assertEqual(task.coverage['stop_reason']['category'], 'source_denied_or_rate_limited')

    def test_unknown_exception_does_not_inherit_prior_safe_state(self):
        self.provider.download.side_effect = [mixed(), RuntimeError('unknown'), prices()]
        task = self.task(symbols=['RELIANCE.NS', 'TCS.NS', 'INFY.NS'])
        self.assertFalse(task.run())
        self.assertEqual(self.provider.download.call_count, 2)
        self.assertEqual(task.coverage['row_quality_rejected'], ['RELIANCE.NS'])
        self.assertEqual(task.coverage['stop_reason']['symbol'], 'TCS.NS')
        self.assertNotIn('TCS.NS', task.coverage['observations'])

    def test_empty_response_does_not_inherit_prior_safe_state(self):
        self.provider.download.side_effect = [mixed(), pd.DataFrame(), prices()]
        task = self.task(symbols=['RELIANCE.NS', 'TCS.NS', 'INFY.NS'])
        self.assertFalse(task.run())
        self.assertEqual(self.provider.download.call_count, 2)
        self.assertEqual(task.coverage['stop_reason']['symbol'], 'TCS.NS')
        self.assertEqual(task.coverage['not_attempted'], ['INFY.NS'])

    def test_reused_object_resets_attempts_and_old_evidence_is_immutable(self):
        self.provider.download.side_effect = [mixed(), prices()]
        task = self.task()
        self.assertFalse(task.run())
        path = Path(task.rejection_evidence[0]['manifest'])
        before = path.read_bytes()
        self.provider.download.side_effect = [prices(), prices()]
        self.assertTrue(task.run())
        self.assertEqual(task.coverage['attempted'], task.symbols)
        self.assertEqual(task.coverage['row_quality_rejected'], [])
        self.assertEqual(task.rejection_evidence, [])
        self.assertEqual(path.read_bytes(), before)

    def test_switch_is_a_real_boolean_not_a_truthy_string(self):
        for value in ('false', 'true', 0, 1, None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.task(enabled=value)


if __name__ == '__main__':
    unittest.main()

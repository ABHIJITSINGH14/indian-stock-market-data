"""Offline row-conservation, exact rejection and non-admission regression tests."""
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from utils.price_evidence import audit_price_frame, write_price_evidence
from test_yahoo_sources import load_adapter, prices

START, END = date(2026, 9, 14), date(2026, 9, 19)


class PriceEvidenceTests(unittest.TestCase):
    def audit(self, frame):
        result = audit_price_frame(frame, START, END)
        positions = result.accepted_positions + [r['row_position'] for r in result.rejected]
        self.assertEqual(sorted(positions), list(range(len(frame))))
        self.assertEqual(len(set(positions)), len(frame))
        return result

    def test_valid_frame_is_not_rejected(self):
        self.assertTrue(self.audit(prices()).passed)

    def test_nan_is_preserved_with_date_and_field(self):
        frame = prices()
        frame.loc[frame.index[0], 'Open'] = float('nan')
        before = frame.copy(deep=True)
        result = self.audit(frame)
        self.assertEqual(result.accepted_positions, [1])
        row = result.rejected[0]
        self.assertEqual(row['date'], '2026-09-14T00:00:00')
        self.assertIn('missing:Open', row['reasons'])
        self.assertEqual(row['cells'][0]['repr'], repr(frame.iloc[0, 0]))
        pd.testing.assert_frame_equal(frame, before)

    def test_all_offending_fields_are_recorded(self):
        frame = prices()
        frame.iloc[0, 0] = float('nan')
        frame.iloc[0, 1] = float('inf')
        frame.iloc[0, 2] = -1
        frame.iloc[0, 5] = -2
        result = self.audit(frame)
        self.assertEqual(set(result.rejected[0]['reasons']),
                         {'missing:Open', 'nonfinite:High', 'nonpositive:Low', 'negative:Volume'})

    def test_zero_volume_can_be_usable_but_zero_open_cannot(self):
        frame = prices()
        frame['Volume'] = 0
        frame.iloc[0, 0] = 0
        result = self.audit(frame)
        self.assertEqual(result.accepted_positions, [1])
        self.assertEqual(result.rejected[0]['reasons'], ['nonpositive:Open'])

    def test_fractional_volume_quarantined(self):
        frame = prices()
        frame['Volume'] = [1.5, 2.0]
        self.assertEqual(self.audit(frame).rejected[0]['reasons'], ['fractional:Volume'])

    def test_duplicate_dates_quarantine_both_copies(self):
        frame = pd.concat([prices(), prices().iloc[[0]]])
        result = self.audit(frame)
        self.assertEqual(result.accepted_positions, [1])
        self.assertEqual([x['row_position'] for x in result.rejected], [0, 2])
        self.assertTrue(all('duplicate_date' in x['reasons'] for x in result.rejected))

    def test_duplicate_calendar_day_with_different_time_is_rejected(self):
        frame = prices()
        frame.index = pd.to_datetime(['2026-09-14 00:00', '2026-09-14 01:00'])
        self.assertEqual(self.audit(frame).accepted_positions, [])

    def test_missing_date_and_exclusive_end_are_rejected(self):
        frame = prices()
        frame.index = pd.to_datetime([None, '2026-09-19'])
        result = self.audit(frame)
        self.assertEqual(result.rejected[0]['reasons'], ['missing_date'])
        self.assertEqual(result.rejected[1]['reasons'], ['date_outside_request'])

    def test_frame_schema_is_not_guessed(self):
        frame = prices().drop(columns='Close')
        result = self.audit(frame)
        self.assertEqual(result.accepted_positions, [])
        self.assertIn('missing_ohlcv_columns', result.frame_errors)

    def test_duplicate_columns_preserved_in_typed_cell_list(self):
        frame = prices()
        frame.columns = ['X'] * len(frame.columns)
        result = self.audit(frame)
        self.assertEqual(len(result.rejected[0]['cells']), 6)
        self.assertEqual(result.frame_errors, ['ambiguous_columns'])

    def test_object_dtype_never_silently_coerced(self):
        frame = prices().astype(object)
        result = self.audit(frame)
        self.assertEqual(result.accepted_positions, [])
        self.assertIn('nonnumeric_column:Open', result.frame_errors)

    def test_complex_dtype_is_not_a_price(self):
        frame = prices()
        frame['Close'] = [1 + 2j, 3 + 4j]
        self.assertEqual(self.audit(frame).accepted_positions, [])

    def test_optional_adjusted_close_can_remain_missing(self):
        frame = prices()
        frame['Adj Close'] = [float('nan'), 6.0]
        self.assertTrue(self.audit(frame).passed)

    def test_invalid_adjusted_close_rejects_only_its_row(self):
        frame = prices()
        frame['Adj Close'] = ['bad', '6.0']
        result = self.audit(frame)
        self.assertEqual(result.accepted_positions, [1])
        self.assertEqual(result.rejected[0]['reasons'], ['invalid:Adj Close'])

    def test_ohlc_ordering_failure_is_not_repaired(self):
        frame = prices()
        frame.iloc[0, 1] = 1
        self.assertEqual(self.audit(frame).rejected[0]['reasons'], ['ohlc_order'])

    def test_nullable_numeric_missing_values_are_rejected(self):
        frame = prices()
        frame['Open'] = pd.array([pd.NA, 11], dtype='Float64')
        self.assertEqual(self.audit(frame).rejected[0]['reasons'], ['missing:Open'])

    def test_rows_with_no_columns_are_conserved(self):
        frame = pd.DataFrame(index=prices().index)
        self.assertEqual(len(self.audit(frame).rejected), 2)

    def test_empty_and_non_dataframe_are_explicit(self):
        for value in (None, 'HTML', []):
            self.assertEqual(audit_price_frame(value, START, END).frame_errors, ['not_a_dataframe'])
        self.assertEqual(self.audit(pd.DataFrame()).frame_errors, ['empty_frame'])

    def test_evidence_hashes_and_usable_subset_exactly_match_input(self):
        frame = prices()
        frame.iloc[0, 0] = float('nan')
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {
            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_SHA': 'a' * 40,
        }):
            path, manifest = write_price_evidence(frame, self.audit(frame), root,
                                                  {'provider_symbol': 'RELIANCE.NS'})
            self.assertEqual(manifest['frame_rows'], 2)
            self.assertEqual(manifest['usable_rows'], 1)
            self.assertEqual(manifest['rejected_rows'], 1)
            self.assertEqual(manifest['run_attempt'], '2')
            self.assertFalse(manifest['collector_admission'])
            self.assertEqual(manifest['historical_completeness'], 'not_verified')
            for name, item in manifest['files'].items():
                payload = (path / name).read_bytes()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), item['sha256'])
                self.assertEqual(len(payload), item['bytes'])
            self.assertEqual((path / 'provider-frame.csv').read_text(), frame.to_csv())
            self.assertEqual((path / 'usable-observations.csv').read_text(), frame.iloc[[1]].to_csv())
            json.loads((path / 'rejected-rows.jsonl').read_text())
            self.assertEqual(json.loads((path / 'manifest.json').read_text()), manifest)

    def test_failed_write_has_no_committed_manifest(self):
        frame = prices()
        with tempfile.TemporaryDirectory() as root, patch.object(Path, 'write_bytes', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                write_price_evidence(frame, self.audit(frame), root, {})
            self.assertEqual(list(Path(root).rglob('manifest.json')), [])

    def test_mixed_frame_stays_failed_and_never_becomes_production_csv(self):
        cls, provider = load_adapter('download_nse_data.py', 'NSEDataDownloader')
        frame = prices()
        frame.iloc[0, 0] = float('nan')
        provider.download.return_value = frame
        with tempfile.TemporaryDirectory() as root:
            task = cls(symbols=['RELIANCE.NS', 'TCS.NS'], start_date=START, end_date=END,
                       output_path=Path(root, 'production.csv'))
            self.assertFalse(task.run())
            self.assertEqual(provider.download.call_count, 1)
            self.assertFalse(task.output_path.exists())
            self.assertEqual(task.coverage['returned'], [])
            evidence = task.coverage['rejection_evidence'][0]
            self.assertEqual(evidence['usable_rows'], 1)
            self.assertEqual(evidence['rejected_rows'], 1)
            self.assertTrue(Path(evidence['manifest']).exists())

    def test_reused_collector_keeps_old_evidence_immutable_not_in_new_coverage(self):
        cls, provider = load_adapter('download_nse_data.py', 'NSEDataDownloader')
        frame = prices()
        frame.iloc[0, 0] = float('nan')
        provider.download.return_value = frame
        with tempfile.TemporaryDirectory() as root:
            task = cls(symbols=['RELIANCE.NS'], start_date=START, end_date=END,
                       output_path=Path(root, 'production.csv'))
            self.assertFalse(task.run())
            first = Path(task.rejection_evidence[0]['manifest'])
            old = first.read_bytes()
            self.assertFalse(task.run())
            self.assertEqual(first.read_bytes(), old)
            self.assertNotEqual(str(first), task.rejection_evidence[0]['manifest'])
            self.assertEqual(len(task.coverage['rejection_evidence']), 1)
            provider.download.return_value = prices()
            self.assertTrue(task.run())
            self.assertEqual(task.coverage['rejection_evidence'], [])


if __name__ == '__main__':
    unittest.main()

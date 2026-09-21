"""Partition provider frames for diagnosis, never repair or certify price history."""
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

REQUIRED = ('Open', 'High', 'Low', 'Close', 'Volume')


@dataclass
class PriceAudit:
    accepted_positions: list
    rejected: list
    frame_errors: list

    @property
    def passed(self):
        return bool(self.accepted_positions) and not self.rejected and not self.frame_errors


def _cell(value):
    """Typed representation: distinguish a missing value from zero or a string."""
    return {'type': type(value).__name__, 'repr': repr(value)}


def audit_price_frame(frame, start, end):
    """Return an exhaustive, disjoint row partition without changing the input.

    Ambiguous schema/dtypes fail the entire frame. Within a valid schema, each
    invalid date/value quarantines its row; both copies of a duplicate date are
    rejected. Accepted means these structural checks only, not exchange truth.
    """
    if not isinstance(frame, pd.DataFrame):
        return PriceAudit([], [], ['not_a_dataframe'])
    if len(frame) == 0:
        return PriceAudit([], [], ['empty_frame'])
    errors = []
    if isinstance(frame.columns, pd.MultiIndex) or frame.columns.has_duplicates:
        errors.append('ambiguous_columns')
    elif not set(REQUIRED).issubset(frame.columns):
        errors.append('missing_ohlcv_columns')
    else:
        for name in REQUIRED:
            dtype = frame[name].dtype
            if (not pd.api.types.is_numeric_dtype(dtype)
                    or pd.api.types.is_bool_dtype(dtype)
                    or pd.api.types.is_complex_dtype(dtype)):
                errors.append('nonnumeric_column:' + name)
    if not isinstance(frame.index, pd.DatetimeIndex):
        errors.append('not_a_datetime_index')

    reasons = [list(errors) for _ in range(len(frame))]
    if not errors:
        days = [None if pd.isna(value) else value.date() for value in frame.index]
        counts = Counter(day for day in days if day is not None)
        for pos, day in enumerate(days):
            if day is None:
                reasons[pos].append('missing_date')
            else:
                if not start <= day < end:
                    reasons[pos].append('date_outside_request')
                if counts[day] > 1:
                    reasons[pos].append('duplicate_date')
        # Check per cell; never let a single NaN hide the other offending fields.
        for name in REQUIRED:
            for pos, value in enumerate(frame[name]):
                if pd.isna(value):
                    reasons[pos].append('missing:' + name)
                elif not math.isfinite(value):
                    reasons[pos].append('nonfinite:' + name)
                elif name == 'Volume':
                    if value < 0:
                        reasons[pos].append('negative:Volume')
                    elif value % 1 != 0:
                        reasons[pos].append('fractional:Volume')
                elif value <= 0:
                    reasons[pos].append('nonpositive:' + name)
        if 'Adj Close' in frame:
            for pos, value in enumerate(frame['Adj Close']):
                if pd.isna(value):
                    continue  # Optional provider adjustment remains missing.
                try:
                    numeric = pd.to_numeric(value, errors='raise')
                    valid = (not isinstance(value, bool)
                             and not isinstance(numeric, complex)
                             and math.isfinite(numeric) and numeric > 0)
                except (TypeError, ValueError, OverflowError):
                    valid = False
                if not valid:
                    reasons[pos].append('invalid:Adj Close')
        for pos in range(len(frame)):
            values = [frame[name].iloc[pos] for name in ('Open', 'High', 'Low', 'Close')]
            if all(not pd.isna(x) and math.isfinite(x) and x > 0 for x in values):
                op, high, low, close = values
                if high < max(op, close, low) or low > min(op, close, high):
                    reasons[pos].append('ohlc_order')

    accepted, rejected = [], []
    for pos, failures in enumerate(reasons):
        if not failures:
            accepted.append(pos)
            continue
        index = frame.index[pos]
        rejected.append({
            'row_position': pos,  # Zero-based source-frame position, not a new date.
            'date': index.isoformat() if isinstance(index, pd.Timestamp) and not pd.isna(index) else None,
            'index': _cell(index),
            'reasons': failures,
            # List rather than dict: even duplicate source labels remain visible.
            'cells': [{'column': repr(name), **_cell(frame.iloc[pos, col])}
                      for col, name in enumerate(frame.columns)],
        })
    return PriceAudit(accepted, rejected, errors)


def write_price_evidence(frame, audit, directory, metadata):
    """Append a fresh evidence bundle; manifest is the final commit marker.

    CSV is a serialization of yfinance's DataFrame, NOT raw exchange/HTTP bytes.
    Typed rejected-cell records preserve NaN/NA/Inf/zero distinctions. Valid
    subsets are diagnostic only and never replace the production output file.
    """
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    # No shared filenames across retries, issuers, or reused collector objects.
    target = Path(tempfile.mkdtemp(prefix='observation-', dir=root))
    files = {}
    if isinstance(frame, pd.DataFrame):
        for name, part in [('provider-frame.csv', frame),
                           ('usable-observations.csv', frame.iloc[audit.accepted_positions])]:
            payload = part.to_csv(index=True).encode('utf-8')
            (target / name).write_bytes(payload)
            files[name] = {'sha256': hashlib.sha256(payload).hexdigest(),
                           'bytes': len(payload), 'rows': len(part)}
    payload = ''.join(json.dumps(row, allow_nan=False) + '\n' for row in audit.rejected).encode('utf-8')
    (target / 'rejected-rows.jsonl').write_bytes(payload)
    files['rejected-rows.jsonl'] = {'sha256': hashlib.sha256(payload).hexdigest(),
                                   'bytes': len(payload), 'rows': len(audit.rejected)}
    manifest = {
        **metadata,
        'schema_version': 1,
        'kind': 'price_rejection_evidence_not_a_complete_dataset',
        'historical_completeness': 'not_verified',
        'collector_admission': False,
        'run_id': os.environ.get('GITHUB_RUN_ID'),
        'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
        'commit_sha': os.environ.get('GITHUB_SHA'),
        'frame_rows': len(frame) if isinstance(frame, pd.DataFrame) else 0,
        'usable_rows': len(audit.accepted_positions),
        'rejected_rows': len(audit.rejected),
        'usable_row_positions': audit.accepted_positions,
        'frame_errors': audit.frame_errors,
        'rejection_counts': dict(Counter(reason for row in audit.rejected for reason in row['reasons'])),
        'column_dtypes': [str(d) for d in frame.dtypes] if isinstance(frame, pd.DataFrame) else [],
        'serialization': 'yfinance DataFrame CSV plus typed rejected cells; not raw HTTP bytes',
        'files': files,
    }
    temporary = target / 'manifest.json.tmp'
    temporary.write_text(json.dumps(manifest, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(target / 'manifest.json')
    return target, manifest

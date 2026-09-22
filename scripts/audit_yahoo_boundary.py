"""Bounded, non-mutating observation of the pinned Yahoo -> DataFrame boundary.

Diagnostic only: never repairs prices or writes production data. The interception
uses yfinance 1.7.0 internals, so another version must be reviewed explicitly.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

VERSION = '1.7.0'
MAX_BYTES = 2_000_000
FIELDS = {'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close',
          'volume': 'Volume', 'adjclose': 'Adj Close'}


class BoundaryError(ValueError):
    """A safe, fixed diagnostic code, never a provider-generated message."""


def request_scope(symbol, start, end):
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9&.\-]*\.NS', symbol):
        raise BoundaryError('invalid_symbol')
    if not 0 < (end - start).days <= 10:
        raise BoundaryError('window_must_be_one_to_ten_calendar_days')
    tz = ZoneInfo('Asia/Kolkata')
    epoch = lambda day: int(datetime.combine(day, datetime.min.time(), tz).timestamp())
    return {'symbol': symbol, 'start': start.isoformat(), 'end_exclusive': end.isoformat(),
            'period1': epoch(start), 'period2': epoch(end), 'interval': '1d'}


def chart_request(url, params, scope):
    parsed = urlsplit(url)
    return (parsed.scheme == 'https' and parsed.hostname in
            {'query1.finance.yahoo.com', 'query2.finance.yahoo.com'} and
            parsed.path == '/v8/finance/chart/' + scope['symbol'] and
            isinstance(params, dict) and all(params.get(k) == scope[k]
                                            for k in ('period1', 'period2', 'interval')))


def decode_chart(body, scope):
    """Validate the response's identity and shape before saving any HTTP body."""
    if len(body) > MAX_BYTES:
        raise BoundaryError('chart_body_too_large')
    obj = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(
        BoundaryError('nonstandard_json_number')))
    chart = obj.get('chart') if isinstance(obj, dict) else None
    if not isinstance(chart, dict) or chart.get('error') is not None:
        raise BoundaryError('chart_error_or_missing')
    results = chart.get('result')
    if not isinstance(results, list) or len(results) != 1:
        raise BoundaryError('ambiguous_chart_results')
    result = results[0]
    meta = result['meta']
    if (meta.get('symbol') != scope['symbol'] or meta.get('currency') != 'INR' or
            meta.get('exchangeTimezoneName') != 'Asia/Kolkata' or
            meta.get('instrumentType') != 'EQUITY' or meta.get('dataGranularity') != '1d'):
        raise BoundaryError('chart_identity_mismatch')
    timestamps = result.get('timestamp')
    if (not isinstance(timestamps, list) or not timestamps or
            any(type(t) is not int for t in timestamps) or
            len(set(timestamps)) != len(timestamps)):
        raise BoundaryError('invalid_chart_timestamps')
    indicators = result['indicators']
    if len(indicators.get('quote', [])) != 1 or len(indicators.get('adjclose', [])) != 1:
        raise BoundaryError('ambiguous_chart_indicators')
    arrays = {**indicators['quote'][0], 'adjclose': indicators['adjclose'][0]['adjclose']}
    for key in FIELDS:
        values = arrays.get(key)
        if not isinstance(values, list) or len(values) != len(timestamps):
            raise BoundaryError('chart_array_length_mismatch')
        if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v))
               for v in values):
            raise BoundaryError('invalid_chart_cell_type')
    rows = {}
    for i, stamp in enumerate(timestamps):
        day = datetime.fromtimestamp(stamp, timezone.utc).astimezone(
            ZoneInfo(meta['exchangeTimezoneName'])).date().isoformat()
        if not scope['start'] <= day < scope['end_exclusive'] or day in rows:
            raise BoundaryError('chart_date_scope_or_duplicate')
        rows[day] = {key: arrays[key][i] for key in FIELDS}
    return rows


def compare_frame(raw_rows, frame):
    if (not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.DatetimeIndex)
            or frame.index.hasnans or frame.index.has_duplicates or frame.columns.has_duplicates
            or not set(FIELDS.values()) <= set(frame.columns)):
        raise BoundaryError('ambiguous_adapter_frame')
    dates = [d.isoformat() for d in frame.index.date]
    if len(set(dates)) != len(dates):
        raise BoundaryError('duplicate_adapter_dates')
    by_day = dict(zip(dates, range(len(frame))))
    compared = []
    for day in sorted(set(raw_rows) | set(by_day)):
        if day not in raw_rows or day not in by_day:
            compared.append({'date': day, 'field': None,
                             'classification': 'adapter_only_date' if day not in raw_rows else 'source_only_date'})
            continue
        for key, column in FIELDS.items():
            raw = raw_rows[day][key]
            cell = frame[column].iloc[by_day[day]]
            missing = bool(pd.isna(cell))
            value = None if missing else float(cell)
            if value is not None and not math.isfinite(value):
                raise BoundaryError('nonfinite_adapter_cell')
            if raw is None:
                classification = 'missing_in_source_and_frame' if missing else 'source_missing_adapter_changed'
            elif missing:
                classification = 'adapter_only_missing'
            else:
                classification = 'agree' if raw == value else 'value_changed'
            compared.append({'date': day, 'field': key, 'source_value': raw,
                             'adapter_value': value, 'classification': classification})
    return compared


class ChartObserver:
    """Observe matching completed GET responses; do not change args or response."""
    def __init__(self, original, scope):
        self.original, self.scope = original, scope
        self.responses = []

    def get(self, instance, *args, **kwargs):
        response = self.original(instance, *args, **kwargs)
        url = kwargs.get('url', args[0] if args else '')
        params = kwargs.get('params', args[1] if len(args) > 1 else None)
        if chart_request(url, params, self.scope):
            # No cookies, request headers, crumbs, signed URLs or error bodies.
            item = {'status_code': response.status_code}
            self.responses.append(item)
            if response.status_code == 200:
                body = response.content
                try:
                    item['rows'] = decode_chart(body, self.scope)
                    item['body'] = body
                except Exception as exc:
                    item['decode_error_type'] = type(exc).__name__
                    if isinstance(exc, BoundaryError):
                        item['decode_error_code'] = str(exc)
        return response


def write_json(path, obj):
    payload = (json.dumps(obj, indent=2, allow_nan=False) + '\n').encode()
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('xb') as handle:
        handle.write(payload)
    temporary.replace(path)


def run_probe(directory, symbol, start, end):
    scope = request_scope(symbol, start, end)
    import yfinance as yf
    from yfinance.data import YfData
    from scripts import download_nse_data as production
    if yf.__version__ != VERSION:
        raise BoundaryError('unreviewed_yfinance_version')
    directory.mkdir(parents=True, exist_ok=False)
    result = {'kind': 'yahoo_boundary_diagnostic_not_production', 'request': scope,
              'provider_version': yf.__version__, 'run_id': os.getenv('GITHUB_RUN_ID'),
              'run_attempt': os.getenv('GITHUB_RUN_ATTEMPT'), 'commit_sha': os.getenv('GITHUB_SHA'),
              'captured_at_utc': datetime.now(timezone.utc).isoformat(),
              'probe_status': 'incomplete', 'historical_completeness': 'not_verified'}
    observed_frames, audits = [], []
    original_audit = production.audit_price_frame
    def inspect_frame(frame, *args, **kwargs):
        observed_frames.append(frame.copy(deep=True) if isinstance(frame, pd.DataFrame) else frame)
        audited = original_audit(frame, *args, **kwargs)
        audits.append(audited)
        return audited
    observer = ChartObserver(YfData.get, scope)
    def observed_get(instance, *args, **kwargs):
        return observer.get(instance, *args, **kwargs)
    try:
        collector = production.NSEDataDownloader(symbols=[symbol], start_date=start,
            end_date=end, output_path=directory / 'unused-production-output.csv')
        with patch.object(YfData, 'get', new=observed_get), \
             patch.object(production, 'audit_price_frame', side_effect=inspect_frame):
            collector.download_stock_data(symbol)  # One bounded adapter invocation. No save_data/run.
        result['matching_responses'] = [{k: v for k, v in r.items() if k not in ('body', 'rows')}
                                        for r in observer.responses]
        if len(observer.responses) != 1 or 'body' not in observer.responses[0]:
            raise BoundaryError('expected_one_valid_matching_response')
        response = observer.responses[0]
        (directory / 'chart-response.json').write_bytes(response['body'])
        if len(observed_frames) != 1 or len(audits) != 1:
            raise BoundaryError('expected_one_adapter_frame')
        frame = observed_frames[0]
        frame.to_csv(directory / 'adapter-frame.csv', index_label='Date')
        comparisons = compare_frame(response['rows'], frame)
        result.update(probe_status='complete', source_rows=len(response['rows']),
                      adapter_rows=len(frame), adapter_contract_passed=audits[0].passed,
                      comparison_counts=dict(Counter(r['classification'] for r in comparisons)),
                      comparisons=comparisons)
    except Exception as exc:
        # Error types only: exception messages from providers may contain credentials.
        result['error_type'] = type(exc).__name__
        if isinstance(exc, BoundaryError):
            result['error_code'] = str(exc)
    finally:
        result['files'] = {p.relative_to(directory).as_posix():
            {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(directory.rglob('*')) if p.is_file() and not p.name.endswith('.tmp')}
        write_json(directory / 'manifest.json', result)
    # A completed diagnostic is not necessarily a healthy data response.
    return 0 if result['probe_status'] == 'complete' and result['adapter_contract_passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--start', type=date.fromisoformat, required=True)
    parser.add_argument('--end', type=date.fromisoformat, required=True)
    args = parser.parse_args()
    return run_probe(args.directory, args.symbol, args.start, args.end)


if __name__ == '__main__':
    raise SystemExit(main())

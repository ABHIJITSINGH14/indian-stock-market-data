"""Yahoo-sourced NSE-listed prices; the legacy output name is not an exchange bhavcopy."""
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

from config.config import START_DATE, END_DATE, BHAVCOPY_CSV_PATH
from config.issuer_identity import reject_known_typo, historical_price_deferral
from utils.logger import setup_logger
from utils.data_processor import DataProcessor
from utils.price_evidence import audit_price_frame, write_price_evidence

logger = setup_logger(__name__)


class NSEDataDownloader:
    # Preserve the requested universe: do not silently replace legacy issuers.
    NSE_STOCKS = [
        'RELIANCE.NS', 'TCS.NS', 'INFY.NS', 'WIPRO.NS', 'HDFC.NS',
        'ICICIBANK.NS', 'SBIN.NS', 'MARUTI.NS', 'BAJAJFINSV.NS', 'TITAN.NS',
        'LT.NS', 'NESTLEIND.NS', 'ASIANPAINT.NS', 'SUNPHARMA.NS', 'DRREDDY.NS',
        'CIPLA.NS', 'DMART.NS', 'POWERGRID.NS', 'ULTRACEMCO.NS', 'COALINDIA.NS'
    ]

    def __init__(self, symbols=None, start_date=None, end_date=None, output_path=None,
                 *, continue_after_row_rejection=False, defer_archival_prices=False):
        if not isinstance(continue_after_row_rejection, bool):
            raise TypeError("continue_after_row_rejection must be a bool")
        if not isinstance(defer_archival_prices, bool):
            raise TypeError("defer_archival_prices must be a bool")
        self.defer_archival_prices = defer_archival_prices
        self.continue_after_row_rejection = continue_after_row_rejection
        self._last_failure_kind = None
        self._last_observation = None
        self.symbols = list(self.NSE_STOCKS if symbols is None else symbols)
        if not self.symbols or len(set(self.symbols)) != len(self.symbols) or any(
            not isinstance(s, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9&.\-]*\.NS', s)
            for s in self.symbols
        ):
            raise ValueError('Provide distinct, explicit NSE Yahoo ticker identities')
        for symbol in self.symbols:
            reject_known_typo(symbol)
        self.start_date = date.fromisoformat(str(START_DATE if start_date is None else start_date))
        self.end_date = date.fromisoformat(str(END_DATE if end_date is None else end_date))
        if self.start_date >= self.end_date:
            raise ValueError('Start must precede exclusive end date')
        self.output_path = Path(BHAVCOPY_CSV_PATH if output_path is None else output_path)
        self.processor = DataProcessor()
        self.all_data = pd.DataFrame()
        self.coverage = {}
        self.evidence_dir = self.output_path.parent / "price-rejections"
        self.rejection_evidence = []

    def download_stock_data(self, symbol):
        self._last_failure_kind = None
        self._last_observation = None
        try:
            logger.info('Downloading Yahoo prices for %s', symbol)
            frame = yf.download(
                symbol, start=self.start_date, end=self.end_date, interval='1d',
                auto_adjust=False, back_adjust=False, repair=False, rounding=False,
                keepna=True, multi_level_index=False, threads=False,
                progress=False, timeout=30,
            )
            audit = audit_price_frame(frame, self.start_date, self.end_date)
            if not audit.passed:
                target, manifest = write_price_evidence(frame, audit, self.evidence_dir, {
                    'source': 'yahoo_finance', 'provider_symbol': symbol,
                    'provider_version': yf.__version__,
                    'retrieved_at_utc': datetime.now(timezone.utc).isoformat(),
                    'requested_start': self.start_date.isoformat(),
                    'requested_end_exclusive': self.end_date.isoformat(),
                    'price_basis': 'provider_ohlc_no_additional_yfinance_adjustment',
                })
                record = {'symbol': symbol, 'manifest': str(target / 'manifest.json'),
                          'frame_rows': manifest['frame_rows'],
                          'usable_rows': manifest['usable_rows'],
                          'rejected_rows': manifest['rejected_rows'],
                          'frame_errors': manifest['frame_errors'],
                          'rejection_counts': manifest['rejection_counts']}
                self.rejection_evidence.append(record)
                # Continue only after a persisted, unambiguous, nonempty response
                # with usable rows and numerical row defects. Date/schema faults,
                # entirely unusable responses and evidence-write errors stop us.
                numeric_reasons = {
                    'missing:' + field for field in ('Open', 'High', 'Low', 'Close', 'Volume')
                } | {
                    'nonfinite:' + field for field in ('Open', 'High', 'Low', 'Close', 'Volume')
                } | {
                    'nonpositive:' + field for field in ('Open', 'High', 'Low', 'Close')
                } | {'negative:Volume', 'fractional:Volume', 'invalid:Adj Close', 'ohlc_order'}
                can_continue = (not audit.frame_errors and bool(audit.accepted_positions)
                                and bool(audit.rejected) and all(
                                    row['reasons'] and set(row['reasons']) <= numeric_reasons
                                    for row in audit.rejected))
                self._last_failure_kind = ('row_quality_rejected' if can_continue
                                           else 'unverified_response')
                self._last_observation = {**record, 'status': self._last_failure_kind}
                logger.error('Price admission failed; evidence: %s', record)
                return None  # Diagnostic recovery never changes whole-frame admission.
            result = frame.copy().sort_index()
            result.index.name = 'Date'
            result = result.reset_index()
            result['Date'] = result['Date'].dt.strftime('%Y-%m-%d')
            result['Symbol'] = symbol[:-3]
            result['source'] = 'yahoo_finance'
            result['provider_symbol'] = symbol
            result['provider_version'] = yf.__version__
            result['price_basis'] = 'provider_ohlc_no_additional_yfinance_adjustment'
            result['retrieved_at_utc'] = datetime.now(timezone.utc).isoformat()
            self._last_observation = {
                'symbol': symbol, 'status': 'admitted_frame',
                'frame_rows': len(frame), 'usable_rows': len(frame), 'rejected_rows': 0,
            }
            return result
        except Exception as exc:
            # yfinance's dedicated exception is independent of the HTTP backend.
            if type(exc).__name__ == 'YFRateLimitError' or getattr(
                getattr(exc, 'response', None), 'status_code', None
            ) in (401, 403, 429):
                self._last_failure_kind = 'source_denied_or_rate_limited'
                raise
            self._last_failure_kind = 'source_or_processing_error'
            logger.error('Unverified Yahoo prices for %s: %s', symbol, exc)
            return None

    def _persist_archival_plan(self, requests, planned_at):
        """Finalize outstanding work before bypassing any scheduled lookup.

        This manifest is a worklist, not downloaded prices or a functioning
        archive adapter. Keep it separate from both task CSVs and row evidence.
        """
        root = self.output_path.parent / 'price-archive-requests'
        root.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix='plan-', dir=root))
        payload = (json.dumps({
            'schema_version': 1,
            'kind': 'outstanding_price_archival_work_not_market_data',
            'run_id': os.environ.get('GITHUB_RUN_ID'),
            'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
            'commit_sha': os.environ.get('GITHUB_SHA'),
            'planned_at_utc': planned_at.isoformat(),
            'original_requested': list(self.symbols),
            'provider_candidates': [s for s in self.symbols if s not in requests],
            'archival_requests': requests,
            'historical_completeness': 'not_verified',
            'downloaded_rows': 0,
        }, indent=2, allow_nan=False) + '\n').encode('utf-8')
        temporary = target / 'manifest.json.tmp'
        temporary.write_bytes(payload)
        path = temporary.replace(target / 'manifest.json')
        return {'path': str(path), 'sha256': hashlib.sha256(payload).hexdigest(),
                'bytes': len(payload)}

    def download_all_stocks(self):
        self.all_data = pd.DataFrame()
        self.rejection_evidence = []
        self._last_failure_kind = None
        self._last_observation = None
        self.coverage = {'requested': list(self.symbols), 'returned': [],
                         'not_returned': list(self.symbols), 'historical_completeness': 'not_verified',
                         'rejection_evidence': self.rejection_evidence,
                         'continue_after_row_rejection': self.continue_after_row_rejection,
                         'attempted': [], 'not_attempted': list(self.symbols),
                         'row_quality_rejected': [], 'observations': {}, 'stop_reason': None,
                         'defer_archival_prices': self.defer_archival_prices,
                         'deferred_archival': [], 'archival_requests': {}, 'archival_plan': None}
        # Plan by reviewed identity BEFORE any provider call, never in response
        # to an arbitrary empty/denied result. Preserve scope even on early stop.
        planned_at = datetime.now(timezone.utc)
        requests = {}
        if self.defer_archival_prices:
            for symbol in self.symbols:
                request = historical_price_deferral(
                    symbol, self.start_date, self.end_date, planned_at.date())
                if request is not None:
                    requests[symbol] = request
        self.coverage['archival_requests'] = requests
        self.coverage['provider_candidates'] = [s for s in self.symbols if s not in requests]
        self.coverage['pending_provider'] = list(self.coverage['provider_candidates'])
        if requests:
            try:
                self.coverage['archival_plan'] = self._persist_archival_plan(requests, planned_at)
            except Exception:
                self.coverage['stop_reason'] = {'symbol': None,
                                               'category': 'archival_plan_persistence_failed'}
                raise  # No lookup may be deferred without a finalized work record.
            self.coverage['deferred_archival'] = list(requests)
            for symbol in requests:
                self.coverage['observations'][symbol] = {
                    'symbol': symbol, 'status': 'archival_required',
                    'policy_id': requests[symbol]['policy_id'], 'provider_lookup_attempted': False,
                }
        frames = []
        try:
            for index, symbol in enumerate(self.symbols):
                if symbol in requests:
                    logger.warning('Deferred %s to outstanding archival work; history remains incomplete',
                                   symbol)
                    continue
                if index:
                    time.sleep(2)
                self._last_failure_kind = None  # Never reuse a previous issuer's outcome.
                self._last_observation = None
                self.coverage['attempted'].append(symbol)
                self.coverage['not_attempted'].remove(symbol)
                self.coverage['pending_provider'].remove(symbol)
                try:
                    frame = self.download_stock_data(symbol)
                except Exception:
                    self.coverage['stop_reason'] = {
                        'symbol': symbol,
                        'category': self._last_failure_kind or 'source_or_processing_error',
                    }
                    raise
                if self._last_observation is not None:
                    self.coverage['observations'][symbol] = self._last_observation
                if frame is None:
                    category = self._last_failure_kind or 'unverified_response'
                    if category == 'row_quality_rejected':
                        self.coverage['row_quality_rejected'].append(symbol)
                        if self.continue_after_row_rejection:
                            logger.warning('Preserved rejected %s frame; examining next issuer, '
                                           'without admitting this frame or declaring completion', symbol)
                            continue
                    # Empty/ambiguous responses can hide provider-wide denial.
                    self.coverage['stop_reason'] = {'symbol': symbol, 'category': category}
                    logger.error('Stopping after %s for %s; remaining identities stay unverified',
                                 category, symbol)
                    return False
                frames.append(frame)
                self.coverage['returned'].append(symbol)
                self.coverage['not_returned'].remove(symbol)
            return bool(frames) and not self.coverage['not_returned']
        finally:
            if frames:
                self.all_data = pd.concat(frames, ignore_index=True)

    def save_data(self):
        if self.all_data.empty:
            return False
        mapping = {'Date': 'date', 'Open': 'open', 'High': 'high', 'Low': 'low',
                   'Close': 'close', 'Adj Close': 'adj_close', 'Volume': 'volume', 'Symbol': 'symbol'}
        self.all_data = self.processor.clean_data(self.all_data).rename(columns=mapping)
        return self.processor.save_csv(self.all_data, self.output_path)

    def run(self):
        complete = False
        saved = False
        try:
            complete = self.download_all_stocks()
        finally:
            if not self.all_data.empty:
                saved = self.save_data()
        return complete and saved


if __name__ == '__main__':
    raise SystemExit(0 if NSEDataDownloader().run() else 1)

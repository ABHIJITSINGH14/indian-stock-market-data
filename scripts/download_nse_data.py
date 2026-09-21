"""Yahoo-sourced NSE-listed prices; the legacy output name is not an exchange bhavcopy."""
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

from config.config import START_DATE, END_DATE, BHAVCOPY_CSV_PATH
from config.issuer_identity import reject_known_typo
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

    def __init__(self, symbols=None, start_date=None, end_date=None, output_path=None):
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
                          'usable_rows': manifest['usable_rows'],
                          'rejected_rows': manifest['rejected_rows'],
                          'frame_errors': manifest['frame_errors'],
                          'rejection_counts': manifest['rejection_counts']}
                self.rejection_evidence.append(record)
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
            return result
        except Exception as exc:
            # yfinance's dedicated exception is independent of the HTTP backend.
            if type(exc).__name__ == 'YFRateLimitError' or getattr(
                getattr(exc, 'response', None), 'status_code', None
            ) in (401, 403, 429):
                raise
            logger.error('Unverified Yahoo prices for %s: %s', symbol, exc)
            return None

    def download_all_stocks(self):
        self.all_data = pd.DataFrame()
        self.rejection_evidence = []
        self.coverage = {'requested': list(self.symbols), 'returned': [],
                         'not_returned': list(self.symbols), 'historical_completeness': 'not_verified',
                         'rejection_evidence': self.rejection_evidence}
        frames = []
        try:
            for index, symbol in enumerate(self.symbols):
                if index:
                    time.sleep(2)
                frame = self.download_stock_data(symbol)
                if frame is None:
                    # Empty results can hide a provider-wide denial: do not flood remaining tickers.
                    logger.error('Stopping after unverified response; remaining identities stay unverified')
                    return False
                frames.append(frame)
                self.coverage['returned'].append(symbol)
                self.coverage['not_returned'].remove(symbol)
            return bool(frames)
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

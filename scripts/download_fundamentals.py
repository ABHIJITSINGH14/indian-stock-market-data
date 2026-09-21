import pandas as pd
import yfinance as yf
from requests.exceptions import HTTPError
import math
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from config.config import FUNDAMENTALS_CSV_PATH
from config.issuer_identity import reject_known_typo, current_snapshot_deferral
from utils.logger import setup_logger
from utils.data_processor import DataProcessor

logger = setup_logger(__name__)

class FundamentalsDownloader:
    """
    Download company fundamentals and financial ratios
    """
    
    def __init__(self, symbols=None, output_path=None):
        self.symbols = list(self.STOCKS if symbols is None else symbols)
        if not self.symbols or len(set(self.symbols)) != len(self.symbols) or any(
            not isinstance(s, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9&.\-]*\.NS', s)
            for s in self.symbols
        ):
            raise ValueError('Provide distinct, explicit NSE Yahoo ticker identities')
        for symbol in self.symbols:
            reject_known_typo(symbol)
        self.output_path = Path(FUNDAMENTALS_CSV_PATH if output_path is None else output_path)
        self.coverage = {}
        self.last_response_metadata = {}
        self.processor = DataProcessor()
        self.data = pd.DataFrame()
    
    # Major stocks for fundamental analysis
    STOCKS = [
        'RELIANCE.NS', 'TCS.NS', 'INFY.NS', 'WIPRO.NS', 'HDFC.NS',
        'ICICIBANK.NS', 'SBIN.NS', 'MARUTI.NS', 'BAJAJFINSV.NS', 'TITAN.NS',
        'LT.NS', 'NESTLEIND.NS', 'ASIANPAINT.NS', 'SUNPHARMA.NS', 'DRREDDY.NS'
    ]
    
    def fetch_stock_info(self, symbol):
        """
        Fetch fundamental information for a stock
        """
        self.last_response_metadata = {}
        reject_known_typo(symbol)
        deferral = current_snapshot_deferral(symbol, datetime.now(ZoneInfo('Asia/Kolkata')).date())
        if deferral is not None:
            self.last_response_metadata = {'requested_symbol': symbol, **deferral}
            logger.warning('No current standalone snapshot for %s; archival financials remain required', symbol)
            return None
        try:
            logger.info(f"Fetching fundamentals for {symbol}...")
            ticker = yf.Ticker(symbol)
            
            info = ticker.info
            # Record shape/types only: partial quote dictionaries must not masquerade as fundamentals.
            self.last_response_metadata = {
                'requested_symbol': symbol,
                'payload_type': type(info).__name__,
                'field_count': len(info) if isinstance(info, dict) else None,
                'fields': sorted(info) if isinstance(info, dict) else [],
                'market_cap_type': type(info.get('marketCap')).__name__ if isinstance(info, dict) else None,
                'market_cap_present': 'marketCap' in info if isinstance(info, dict) else False,
                'currency': info.get('currency') if isinstance(info, dict) else None,
                'symbol_matches': info.get('symbol') == symbol if isinstance(info, dict) else False,
            }
            if not isinstance(info, dict) or info.get('symbol') != symbol:
                raise ValueError('Missing or mismatched issuer identity')
            cap = info.get('marketCap')
            if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap <= 0:
                raise ValueError('Missing valid market cap; do not manufacture fundamentals')
            if info.get('currency') != 'INR':
                raise ValueError('Market cap currency is not verified as INR')
            
            fundamentals = {
                'symbol': symbol[:-3],
                'provider_symbol': symbol,
                'source': 'yahoo_finance',
                'provider_version': yf.__version__,
                'retrieved_at_utc': datetime.now(timezone.utc).isoformat(),
                'snapshot_only': True,
                'market_cap_currency': info['currency'],
                'financial_currency': info.get('financialCurrency'),
                'name': info.get('longName'),
                'sector': info.get('sector'),
                'industry': info.get('industry'),
                'market_cap': info.get('marketCap'),
                'pe_ratio': info.get('trailingPE'),
                'dividend_yield': info.get('dividendYield'),
                'book_value': info.get('bookValue'),
                'pb_ratio': info.get('priceToBook'),
                'revenue': info.get('totalRevenue'),
                'net_income': info.get('netIncomeToCommon'),
                'roe': info.get('returnOnEquity'),
                'debt_to_equity': info.get('debtToEquity'),
                'current_price': info.get('currentPrice'),
                '52_week_high': info.get('fiftyTwoWeekHigh'),
                '52_week_low': info.get('fiftyTwoWeekLow'),
            }
            
            return fundamentals
        
        except HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403, 429):
                # Stop this task; do not repeat a denied/rate-limited call for every ticker.
                raise
            logger.error(f"Error fetching fundamentals for {symbol}: {str(e)}")
            return None
        except Exception as e:
            if type(e).__name__ == 'YFRateLimitError' or getattr(
                getattr(e, 'response', None), 'status_code', None
            ) in (401, 403, 429):
                raise
            logger.error(f"Error fetching fundamentals for {symbol}: {str(e)}")
            return None
    
    def download_all_fundamentals(self):
        """
        Download fundamentals for all stocks
        """
        self.data = pd.DataFrame()
        self.last_response_metadata = {}
        as_of = datetime.now(ZoneInfo('Asia/Kolkata')).date()
        deferred = {symbol: reason for symbol in self.symbols
                    if (reason := current_snapshot_deferral(symbol, as_of)) is not None}
        self.coverage = {'requested': list(self.symbols), 'returned': [],
                         'not_returned': list(self.symbols), 'historical_completeness': 'not_verified',
                         'snapshot_as_of_date_ist': as_of.isoformat(), 'deferred': deferred}
        all_fundamentals = []
        try:
            attempted = 0
            for symbol in self.symbols:
                if symbol in deferred:
                    logger.warning('Deferred %s: archival financials required; no successor substitution', symbol)
                    continue
                if attempted:
                    time.sleep(2)
                attempted += 1
                fundamentals = self.fetch_stock_info(symbol)
                if fundamentals is None:
                    logger.error('Stopping after unverified fundamentals; remaining issuers stay unverified')
                    return False
                all_fundamentals.append(fundamentals)
                self.coverage['returned'].append(symbol)
                self.coverage['not_returned'].remove(symbol)
            # Deferred requests remain unreturned. Do not redefine full-scope success.
            return bool(all_fundamentals) and not self.coverage['not_returned']
        finally:
            if all_fundamentals:
                self.data = pd.DataFrame(all_fundamentals)

    def save_data(self):
        """
        Save fundamentals to CSV
        """
        if self.data.empty:
            logger.error("No data to save")
            return False
        
        # Clean data
        self.data = self.processor.clean_data(self.data)
        
        # Save to CSV
        return self.processor.save_csv(self.data, self.output_path)
    
    def run(self):
        """
        Execute complete fundamentals download
        """
        complete = False
        saved = False
        try:
            complete = self.download_all_fundamentals()
        finally:
            if not self.data.empty:
                saved = self.save_data()
        return complete and saved


if __name__ == '__main__':
    raise SystemExit(0 if FundamentalsDownloader().run() else 1)

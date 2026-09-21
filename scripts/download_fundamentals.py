import pandas as pd
import yfinance as yf
from requests.exceptions import HTTPError
from config.config import FUNDAMENTALS_CSV_PATH
from utils.logger import setup_logger
from utils.data_processor import DataProcessor

logger = setup_logger(__name__)

class FundamentalsDownloader:
    """
    Download company fundamentals and financial ratios
    """
    
    def __init__(self):
        self.processor = DataProcessor()
        self.data = pd.DataFrame()
    
    # Major stocks for fundamental analysis
    STOCKS = [
        'RELIANCE.NS', 'TCS.NS', 'INFOSY.NS', 'WIPRO.NS', 'HDFC.NS',
        'ICICIBANK.NS', 'SBIN.NS', 'MARUTI.NS', 'BAJAJFINSV.NS', 'TITAN.NS',
        'LT.NS', 'NESTLEIND.NS', 'ASIANPAINT.NS', 'SUNPHARMA.NS', 'DRREDDY.NS'
    ]
    
    def fetch_stock_info(self, symbol):
        """
        Fetch fundamental information for a stock
        """
        try:
            logger.info(f"Fetching fundamentals for {symbol}...")
            ticker = yf.Ticker(symbol)
            
            info = ticker.info
            
            fundamentals = {
                'symbol': symbol.replace('.NS', ''),
                'name': info.get('longName', 'N/A'),
                'sector': info.get('sector', 'N/A'),
                'industry': info.get('industry', 'N/A'),
                'market_cap': info.get('marketCap', 'N/A'),
                'pe_ratio': info.get('trailingPE', 'N/A'),
                'dividend_yield': info.get('dividendYield', 'N/A'),
                'book_value': info.get('bookValue', 'N/A'),
                'pb_ratio': info.get('priceToBook', 'N/A'),
                'revenue': info.get('totalRevenue', 'N/A'),
                'net_income': info.get('netIncomeToCommon', 'N/A'),
                'roe': info.get('returnOnEquity', 'N/A'),
                'debt_to_equity': info.get('debtToEquity', 'N/A'),
                'current_price': info.get('currentPrice', 'N/A'),
                '52_week_high': info.get('fiftyTwoWeekHigh', 'N/A'),
                '52_week_low': info.get('fiftyTwoWeekLow', 'N/A'),
            }
            
            return fundamentals
        
        except HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403, 429):
                # Stop this task; do not repeat a denied/rate-limited call for every ticker.
                raise
            logger.error(f"Error fetching fundamentals for {symbol}: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Error fetching fundamentals for {symbol}: {str(e)}")
            return None
    
    def download_all_fundamentals(self):
        """
        Download fundamentals for all stocks
        """
        all_fundamentals = []
        
        for symbol in self.STOCKS:
            fundamentals = self.fetch_stock_info(symbol)
            if fundamentals:
                all_fundamentals.append(fundamentals)
        
        if all_fundamentals:
            self.data = pd.DataFrame(all_fundamentals)
            logger.info(f"Downloaded fundamentals for {len(self.data)} companies")
            return True
        else:
            logger.error("No fundamentals downloaded")
            return False
    
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
        return self.processor.save_csv(self.data, FUNDAMENTALS_CSV_PATH)
    
    def run(self):
        """
        Execute complete fundamentals download
        """
        logger.info("Starting fundamentals download...")
        if self.download_all_fundamentals():
            return self.save_data()
        return False

if __name__ == '__main__':
    downloader = FundamentalsDownloader()
    if downloader.run():
        logger.info("Fundamentals download completed successfully!")
    else:
        logger.error("Fundamentals download failed!")

import yfinance as yf
import pandas as pd
from datetime import datetime
from config.config import START_DATE, END_DATE, BHAVCOPY_CSV_PATH
from utils.logger import setup_logger
from utils.data_processor import DataProcessor

logger = setup_logger(__name__)

class NSEDataDownloader:
    """
    Download NSE historical stock data using yfinance
    """
    
    def __init__(self):
        self.processor = DataProcessor()
        self.all_data = pd.DataFrame()
    
    # Major NSE stocks
    NSE_STOCKS = [
        'RELIANCE.NS', 'TCS.NS', 'INFOSY.NS', 'WIPRO.NS', 'HDFC.NS',
        'ICICIBANK.NS', 'SBIN.NS', 'MARUTI.NS', 'BAJAJFINSV.NS', 'TITAN.NS',
        'LT.NS', 'NESTLEIND.NS', 'ASIANPAINT.NS', 'SUNPHARMA.NS', 'DRREDDY.NS',
        'CIPLA.NS', 'DMART.NS', 'POWERGRID.NS', 'ULTRACEMCO.NS', 'COALINDIA.NS'
    ]
    
    def download_stock_data(self, symbol):
        """
        Download historical data for a single stock
        """
        try:
            logger.info(f"Downloading data for {symbol}...")
            df = yf.download(
                symbol,
                start=START_DATE,
                end=END_DATE,
                progress=False,
                timeout=30
            )
            
            if df.empty:
                logger.warning(f"No data found for {symbol}")
                return None
            
            # Reset index to make Date a column
            df.reset_index(inplace=True)
            df['Symbol'] = symbol.replace('.NS', '')
            
            logger.info(f"Downloaded {len(df)} records for {symbol}")
            return df
        
        except Exception as e:
            logger.error(f"Error downloading {symbol}: {str(e)}")
            return None
    
    def download_all_stocks(self):
        """
        Download data for all NSE stocks
        """
        all_data = []
        
        for symbol in self.NSE_STOCKS:
            df = self.download_stock_data(symbol)
            if df is not None:
                all_data.append(df)
        
        if all_data:
            self.all_data = pd.concat(all_data, ignore_index=True)
            logger.info(f"Total records downloaded: {len(self.all_data)}")
            return True
        else:
            logger.error("No data downloaded")
            return False
    
    def save_data(self):
        """
        Save downloaded data to CSV
        """
        if self.all_data.empty:
            logger.error("No data to save")
            return False
        
        # Clean data
        self.all_data = self.processor.clean_data(self.all_data)
        
        # Standardize columns
        column_mapping = {
            'Date': 'date',
            'Open': 'open',
            'High': 'high',
            'Low': 'low',
            'Close': 'close',
            'Volume': 'volume',
            'Symbol': 'symbol'
        }
        self.all_data = self.processor.standardize_columns(self.all_data, column_mapping)
        
        # Save to CSV
        return self.processor.save_csv(self.all_data, BHAVCOPY_CSV_PATH)
    
    def run(self):
        """
        Execute complete NSE data download
        """
        logger.info("Starting NSE data download...")
        if self.download_all_stocks():
            return self.save_data()
        return False

if __name__ == '__main__':
    downloader = NSEDataDownloader()
    if downloader.run():
        logger.info("NSE data download completed successfully!")
    else:
        logger.error("NSE data download failed!")

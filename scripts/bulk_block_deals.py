import pandas as pd
from datetime import datetime, timedelta
from config.config import BULK_DEALS_CSV_PATH, BLOCK_DEALS_CSV_PATH
from utils.logger import setup_logger
from utils.api_client import APIClient
from utils.data_processor import DataProcessor

logger = setup_logger(__name__)

class BulkBlockDealsDownloader:
    """
    Download bulk and block deals from NSE
    """
    
    def __init__(self):
        self.client = APIClient()
        self.processor = DataProcessor()
        self.bulk_deals = pd.DataFrame()
        self.block_deals = pd.DataFrame()
    
    def fetch_bulk_deals(self, start_date, end_date):
        """
        Fetch bulk deals from NSE
        """
        try:
            logger.info(f"Fetching bulk deals from {start_date} to {end_date}...")
            
            # NSE Bulk deals URL
            base_url = 'https://www1.nseindia.com/mweb/reports/'
            
            all_deals = []
            current_date = start_date
            
            while current_date <= end_date:
                date_str = current_date.strftime('%d%b%Y')
                url = f'{base_url}bulkdeals_{date_str}.csv'
                
                try:
                    response = self.client.get(url, retry=False)
                    if response and response.status_code == 200:
                        df = pd.read_csv(pd.io.common.StringIO(response.text))
                        all_deals.append(df)
                        logger.info(f"Fetched bulk deals for {date_str}")
                except:
                    pass  # Date might not have any deals
                
                current_date += timedelta(days=1)
            
            if all_deals:
                self.bulk_deals = pd.concat(all_deals, ignore_index=True)
                logger.info(f"Total bulk deals: {len(self.bulk_deals)}")
                return True
        
        except Exception as e:
            logger.error(f"Error fetching bulk deals: {str(e)}")
        
        return False
    
    def fetch_block_deals(self, start_date, end_date):
        """
        Fetch block deals from NSE
        """
        try:
            logger.info(f"Fetching block deals from {start_date} to {end_date}...")
            
            base_url = 'https://www1.nseindia.com/mweb/reports/'
            
            all_deals = []
            current_date = start_date
            
            while current_date <= end_date:
                date_str = current_date.strftime('%d%b%Y')
                url = f'{base_url}blockdeals_{date_str}.csv'
                
                try:
                    response = self.client.get(url, retry=False)
                    if response and response.status_code == 200:
                        df = pd.read_csv(pd.io.common.StringIO(response.text))
                        all_deals.append(df)
                        logger.info(f"Fetched block deals for {date_str}")
                except:
                    pass
                
                current_date += timedelta(days=1)
            
            if all_deals:
                self.block_deals = pd.concat(all_deals, ignore_index=True)
                logger.info(f"Total block deals: {len(self.block_deals)}")
                return True
        
        except Exception as e:
            logger.error(f"Error fetching block deals: {str(e)}")
        
        return False
    
    def save_data(self):
        """
        Save deals to CSV files
        """
        success = True
        
        if not self.bulk_deals.empty:
            self.bulk_deals = self.processor.clean_data(self.bulk_deals)
            success = self.processor.save_csv(self.bulk_deals, BULK_DEALS_CSV_PATH) and success
        
        if not self.block_deals.empty:
            self.block_deals = self.processor.clean_data(self.block_deals)
            success = self.processor.save_csv(self.block_deals, BLOCK_DEALS_CSV_PATH) and success
        
        return success
    
    def run(self):
        """
        Execute complete bulk and block deals download
        """
        logger.info("Starting bulk and block deals download...")
        
        # Download last 90 days of data
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=90)
        
        self.fetch_bulk_deals(start_date, end_date)
        self.fetch_block_deals(start_date, end_date)
        
        return self.save_data()

if __name__ == '__main__':
    downloader = BulkBlockDealsDownloader()
    if downloader.run():
        logger.info("Bulk and block deals download completed successfully!")
    else:
        logger.error("Bulk and block deals download failed!")

import requests
import pandas as pd
from bs4 import BeautifulSoup
from config.config import BSE_DATA_CSV_PATH, BSE_BASE_URL
from utils.logger import setup_logger
from utils.api_client import APIClient
from utils.data_processor import DataProcessor

logger = setup_logger(__name__)

class BSEDataDownloader:
    """
    Download BSE company data and listings
    """
    
    def __init__(self):
        self.client = APIClient()
        self.processor = DataProcessor()
        self.data = pd.DataFrame()
    
    def fetch_bse_equities_list(self):
        """
        Fetch list of BSE listed companies
        """
        try:
            logger.info("Fetching BSE equities list...")
            
            # BSE provides an API for listed companies
            url = 'https://www.bseindia.com/api/meghraj/equitieslist'
            
            response = self.client.get(url)
            if response:
                data = response.json()
                
                # Extract company information
                companies = []
                if 'Table' in data:
                    for item in data['Table']:
                        companies.append({
                            'code': item.get('Scrip Code'),
                            'name': item.get('Scrip Name'),
                            'group': item.get('Group'),
                            'status': item.get('Status'),
                            'isin': item.get('ISIN'),
                        })
                
                self.data = pd.DataFrame(companies)
                logger.info(f"Fetched data for {len(self.data)} companies")
                return True
            
        except Exception as e:
            logger.error(f"Error fetching BSE equities list: {str(e)}")
        
        return False
    
    def fetch_bse_corporate_actions(self):
        """
        Fetch BSE corporate actions (dividends, splits, etc.)
        """
        try:
            logger.info("Fetching BSE corporate actions...")
            
            url = 'https://www.bseindia.com/api/cainfo'
            response = self.client.get(url)
            
            if response:
                data = response.json()
                logger.info(f"Fetched {len(data)} corporate actions")
                return data
        
        except Exception as e:
            logger.error(f"Error fetching corporate actions: {str(e)}")
        
        return None
    
    def save_data(self):
        """
        Save downloaded BSE data to CSV
        """
        if self.data.empty:
            logger.error("No data to save")
            return False
        
        # Clean data
        self.data = self.processor.clean_data(self.data)
        
        # Save to CSV
        return self.processor.save_csv(self.data, BSE_DATA_CSV_PATH)
    
    def run(self):
        """
        Execute complete BSE data download
        """
        logger.info("Starting BSE data download...")
        if self.fetch_bse_equities_list():
            return self.save_data()
        return False

if __name__ == '__main__':
    downloader = BSEDataDownloader()
    if downloader.run():
        logger.info("BSE data download completed successfully!")
    else:
        logger.error("BSE data download failed!")

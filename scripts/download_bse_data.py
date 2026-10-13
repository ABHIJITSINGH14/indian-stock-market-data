import requests
import pandas as pd
import re
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
        self.coverage = {}
    
    def fetch_bse_equities_list(self):
        """
        Fetch list of BSE listed companies
        """
        self.data = pd.DataFrame()  # Reuse must never save a previous successful response.
        self.coverage = {'historical_completeness': 'not_verified', 'admitted_rows': 0}
        stage = 'source_request'
        try:
            logger.info("Fetching BSE equities list...")
            
            # BSE provides an API for listed companies
            url = 'https://www.bseindia.com/api/meghraj/equitieslist'
            
            response = self.client.get(url)
            if response is None:
                raise ValueError('No BSE response received')
            if response is not None:
                stage = 'payload_decode'
                data = response.json()
                stage = 'payload_validation'
                # Extract company information
                companies = []
                if not isinstance(data, dict) or not isinstance(data.get('Table'), list) or not data['Table']:
                    raise ValueError('Missing nonempty BSE company table')
                codes, isins = set(), set()
                for item in data['Table']:
                    if not isinstance(item, dict):
                        raise ValueError('Invalid BSE company row')
                    code, name, isin = item.get('Scrip Code'), item.get('Scrip Name'), item.get('ISIN')
                    if (type(code) not in (int, str) or not re.fullmatch(r'[1-9][0-9]{5}', str(code))
                            or not isinstance(name, str) or not name.strip()
                            or not isinstance(isin, str) or not re.fullmatch(r'IN[A-Z0-9]{9}[0-9]', isin)):
                        raise ValueError('Missing or invalid BSE company identity')
                    if str(code) in codes or isin in isins:
                        raise ValueError('Duplicate BSE company identity')
                    codes.add(str(code))
                    isins.add(isin)
                    companies.append({
                        'code': code, 'name': name,
                        'group': item.get('Group'), 'status': item.get('Status'), 'isin': isin,
                    })
                
                self.data = pd.DataFrame(companies)
                self.coverage['admitted_rows'] = len(self.data)
                logger.info(f"Fetched data for {len(self.data)} companies")
                return True
            
        except Exception as e:
            self.coverage.update(failure_stage=stage, error_type=type(e).__name__)
            logger.error(f"Error fetching BSE equities list: {str(e)}")
        finally:
            self.coverage['source_request'] = dict(self.client.last_request_metadata)
        
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

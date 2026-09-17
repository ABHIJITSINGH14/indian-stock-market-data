import pandas as pd
import requests
from bs4 import BeautifulSoup
from config.config import CORPORATE_ACTIONS_CSV_PATH
from utils.logger import setup_logger
from utils.api_client import APIClient
from utils.data_processor import DataProcessor

logger = setup_logger(__name__)

class CorporateActionsDownloader:
    """
    Download corporate actions (dividends, splits, bonus) from NSE
    """
    
    def __init__(self):
        self.client = APIClient()
        self.processor = DataProcessor()
        self.data = pd.DataFrame()
    
    def fetch_corporate_actions(self):
        """
        Fetch corporate actions from NSE
        """
        try:
            logger.info("Fetching corporate actions...")
            
            # NSE corporate actions URL
            url = 'https://www1.nseindia.com/content/corporate/'
            
            response = self.client.get(url)
            if response:
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Extract corporate action data from the page
                tables = soup.find_all('table')
                
                all_actions = []
                for table in tables:
                    rows = table.find_all('tr')
                    for row in rows[1:]:  # Skip header
                        cols = row.find_all('td')
                        if len(cols) >= 3:
                            action = {
                                'symbol': cols[0].text.strip(),
                                'type': cols[1].text.strip(),
                                'ex_date': cols[2].text.strip(),
                                'value': cols[3].text.strip() if len(cols) > 3 else 'N/A'
                            }
                            all_actions.append(action)
                
                if all_actions:
                    self.data = pd.DataFrame(all_actions)
                    logger.info(f"Fetched {len(self.data)} corporate actions")
                    return True
        
        except Exception as e:
            logger.error(f"Error fetching corporate actions: {str(e)}")
        
        return False
    
    def save_data(self):
        """
        Save corporate actions to CSV
        """
        if self.data.empty:
            logger.error("No data to save")
            return False
        
        # Clean data
        self.data = self.processor.clean_data(self.data)
        
        # Save to CSV
        return self.processor.save_csv(self.data, CORPORATE_ACTIONS_CSV_PATH)
    
    def run(self):
        """
        Execute complete corporate actions download
        """
        logger.info("Starting corporate actions download...")
        if self.fetch_corporate_actions():
            return self.save_data()
        return False

if __name__ == '__main__':
    downloader = CorporateActionsDownloader()
    if downloader.run():
        logger.info("Corporate actions download completed successfully!")
    else:
        logger.error("Corporate actions download failed!")

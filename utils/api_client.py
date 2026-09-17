import requests
import time
from config.config import API_RETRY_ATTEMPTS, API_RETRY_DELAY, API_TIMEOUT, REQUEST_DELAY
from utils.logger import setup_logger

logger = setup_logger(__name__)

class APIClient:
    """
    Generic API client with retry logic and error handling
    """
    def __init__(self):
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
    
    def get(self, url, params=None, retry=True):
        """
        GET request with retry logic
        """
        attempt = 0
        while attempt < API_RETRY_ATTEMPTS:
            try:
                time.sleep(REQUEST_DELAY)
                response = self.session.get(
                    url,
                    params=params,
                    headers=self.headers,
                    timeout=API_TIMEOUT
                )
                response.raise_for_status()
                logger.info(f"Successfully fetched: {url}")
                return response
            except requests.exceptions.RequestException as e:
                attempt += 1
                if attempt < API_RETRY_ATTEMPTS and retry:
                    logger.warning(f"Attempt {attempt} failed for {url}: {str(e)}. Retrying in {API_RETRY_DELAY}s...")
                    time.sleep(API_RETRY_DELAY)
                else:
                    logger.error(f"Failed to fetch {url} after {API_RETRY_ATTEMPTS} attempts: {str(e)}")
                    raise
        return None
    
    def download_file(self, url, save_path):
        """
        Download file from URL and save to disk
        """
        try:
            response = self.get(url, retry=True)
            if response:
                with open(save_path, 'wb') as f:
                    f.write(response.content)
                logger.info(f"File saved to {save_path}")
                return True
        except Exception as e:
            logger.error(f"Error downloading file: {str(e)}")
        return False
    
    def close(self):
        """
        Close the session
        """
        self.session.close()

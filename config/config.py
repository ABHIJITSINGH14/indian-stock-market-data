import os
from datetime import datetime, timedelta

# Directory Configuration
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')
RAW_DATA_DIR = os.path.join(DATA_DIR, 'raw')
PROCESSED_DATA_DIR = os.path.join(DATA_DIR, 'processed')
DATABASE_DIR = os.path.join(DATA_DIR, 'databases')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

# Create directories if they don't exist
for directory in [RAW_DATA_DIR, PROCESSED_DATA_DIR, DATABASE_DIR, LOG_DIR]:
    os.makedirs(directory, exist_ok=True)

# Database Configuration
DATABASE_PATH = os.path.join(DATABASE_DIR, 'stock_market.db')
DATABASE_URL = f'sqlite:///{DATABASE_PATH}'

# Date Configuration
TODAY = datetime.now().date()
START_DATE = TODAY - timedelta(days=365*20)  # 20 years of data
END_DATE = TODAY

# NSE Configuration
NSE_BASE_URL = 'https://www.nseindia.com'
NSE_BHAVCOPY_URL = 'https://www1.nseindia.com/content/historical/EQUITIES/'
NSE_FO_BHAVCOPY_URL = 'https://www1.nseindia.com/content/historical/FO/BHAV/'

# BSE Configuration
BSE_BASE_URL = 'https://www.bseindia.com'
BSE_EQUITIES_URL = 'https://www.bseindia.com/markets/equity/'

# API Configuration
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 2  # seconds
API_TIMEOUT = 30  # seconds
REQUEST_DELAY = 0.5  # seconds between requests

# Logging Configuration
LOG_LEVEL = 'INFO'
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

# File Configuration
BHAVCOPY_CSV_PATH = os.path.join(RAW_DATA_DIR, 'nse_bhavcopy.csv')
BSE_DATA_CSV_PATH = os.path.join(RAW_DATA_DIR, 'bse_data.csv')
FUNDAMENTALS_CSV_PATH = os.path.join(RAW_DATA_DIR, 'fundamentals.csv')
BULK_DEALS_CSV_PATH = os.path.join(RAW_DATA_DIR, 'bulk_deals.csv')
BLOCK_DEALS_CSV_PATH = os.path.join(RAW_DATA_DIR, 'block_deals.csv')
CORPORATE_ACTIONS_CSV_PATH = os.path.join(RAW_DATA_DIR, 'corporate_actions.csv')

# Download Configuration
DOWNLOAD_TIMEOUT = 300  # 5 minutes
CHUNK_SIZE = 8192  # bytes

# Data Processing
DROP_DUPLICATES = True
FILL_MISSING_VALUES = True
VALIDATE_DATA = True

import pandas as pd
import numpy as np
from config.config import DROP_DUPLICATES, FILL_MISSING_VALUES, VALIDATE_DATA
from utils.logger import setup_logger

logger = setup_logger(__name__)

class DataProcessor:
    """
    Process and validate downloaded stock market data
    """
    
    @staticmethod
    def load_csv(file_path):
        """
        Load CSV file into DataFrame
        """
        try:
            df = pd.read_csv(file_path)
            logger.info(f"Loaded {len(df)} rows from {file_path}")
            return df
        except Exception as e:
            logger.error(f"Error loading CSV {file_path}: {str(e)}")
            return None
    
    @staticmethod
    def save_csv(df, file_path):
        """
        Save DataFrame to CSV file
        """
        try:
            df.to_csv(file_path, index=False)
            logger.info(f"Saved {len(df)} rows to {file_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving CSV to {file_path}: {str(e)}")
            return False
    
    @staticmethod
    def clean_data(df):
        """
        Clean data by removing duplicates and handling missing values
        """
        if df is None or df.empty:
            logger.warning("Empty DataFrame received for cleaning")
            return df
        
        original_rows = len(df)
        
        # Drop duplicates
        if DROP_DUPLICATES:
            df = df.drop_duplicates()
            logger.info(f"Removed {original_rows - len(df)} duplicate rows")
        
        # Fill missing values
        if FILL_MISSING_VALUES:
            df = df.fillna(method='ffill').fillna(method='bfill')
            logger.info(f"Filled missing values using forward/backward fill")
        
        return df
    
    @staticmethod
    def validate_data(df):
        """
        Validate data quality
        """
        if df is None or df.empty:
            logger.warning("Empty DataFrame - validation skipped")
            return False
        
        issues = []
        
        # Check for null values
        null_counts = df.isnull().sum()
        if null_counts.sum() > 0:
            issues.append(f"Found {null_counts.sum()} null values")
        
        # Check for duplicates
        if df.duplicated().sum() > 0:
            issues.append(f"Found {df.duplicated().sum()} duplicate rows")
        
        if issues:
            logger.warning(f"Data validation issues: {', '.join(issues)}")
            return False
        
        logger.info("Data validation passed")
        return True
    
    @staticmethod
    def standardize_columns(df, column_mapping):
        """
        Standardize column names across datasets
        """
        try:
            df = df.rename(columns=column_mapping)
            logger.info(f"Standardized column names: {column_mapping}")
            return df
        except Exception as e:
            logger.error(f"Error standardizing columns: {str(e)}")
            return df

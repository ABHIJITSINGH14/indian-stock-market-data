#!/usr/bin/env python
"""
Analysis script for downloaded data
"""

import pandas as pd
import os
from config.config import RAW_DATA_DIR
from utils.logger import setup_logger

logger = setup_logger(__name__)

class DataAnalyzer:
    """
    Analyze downloaded stock market data
    """
    
    def __init__(self):
        self.bhavcopy = None
        self.fundamentals = None
        self.bulk_deals = None
        self.block_deals = None
        self.corporate_actions = None
    
    def load_all_data(self):
        """
        Load all CSV files into memory
        """
        csv_files = {
            'bhavcopy': 'nse_bhavcopy.csv',
            'fundamentals': 'fundamentals.csv',
            'bulk_deals': 'bulk_deals.csv',
            'block_deals': 'block_deals.csv',
            'corporate_actions': 'corporate_actions.csv'
        }
        
        for attr, filename in csv_files.items():
            file_path = os.path.join(RAW_DATA_DIR, filename)
            if os.path.exists(file_path):
                try:
                    df = pd.read_csv(file_path)
                    setattr(self, attr, df)
                    logger.info(f"Loaded {filename}: {len(df)} rows")
                except Exception as e:
                    logger.error(f"Error loading {filename}: {str(e)}")
    
    def analyze_price_trends(self):
        """
        Analyze stock price trends
        """
        if self.bhavcopy is None:
            logger.warning("Bhavcopy data not loaded")
            return
        
        logger.info("\nPrice Trends Analysis:")
        logger.info("-" * 50)
        
        for symbol in self.bhavcopy['symbol'].unique()[:5]:  # Top 5 symbols
            symbol_data = self.bhavcopy[self.bhavcopy['symbol'] == symbol]
            if len(symbol_data) > 0:
                price_change = ((symbol_data['close'].iloc[-1] - symbol_data['close'].iloc[0]) / 
                               symbol_data['close'].iloc[0] * 100)
                logger.info(f"{symbol}: {price_change:.2f}% change")
    
    def analyze_fundamentals(self):
        """
        Analyze company fundamentals
        """
        if self.fundamentals is None:
            logger.warning("Fundamentals data not loaded")
            return
        
        logger.info("\nFundamentals Analysis:")
        logger.info("-" * 50)
        
        # Show top companies by market cap
        try:
            fundamentals_sorted = self.fundamentals.sort_values('market_cap', ascending=False)
            logger.info("Top 5 companies by market cap:")
            for idx, row in fundamentals_sorted.head(5).iterrows():
                logger.info(f"  {row['symbol']}: ₹{row['market_cap']/10**9:.2f}B")
        except Exception as e:
            logger.error(f"Error analyzing fundamentals: {str(e)}")
    
    def generate_report(self):
        """
        Generate data summary report
        """
        logger.info("\n" + "="*50)
        logger.info("DATA SUMMARY REPORT")
        logger.info("="*50)
        
        if self.bhavcopy is not None:
            logger.info(f"\nPrice Data: {len(self.bhavcopy)} records")
            logger.info(f"  Symbols: {self.bhavcopy['symbol'].nunique()}")
            logger.info(f"  Date range: {self.bhavcopy['date'].min()} to {self.bhavcopy['date'].max()}")
        
        if self.fundamentals is not None:
            logger.info(f"\nFundamentals: {len(self.fundamentals)} companies")
        
        if self.bulk_deals is not None:
            logger.info(f"\nBulk Deals: {len(self.bulk_deals)} records")
        
        if self.block_deals is not None:
            logger.info(f"\nBlock Deals: {len(self.block_deals)} records")
        
        if self.corporate_actions is not None:
            logger.info(f"\nCorporate Actions: {len(self.corporate_actions)} records")
        
        logger.info("="*50 + "\n")
    
    def run(self):
        """
        Execute all analysis tasks
        """
        logger.info("Starting data analysis...")
        self.load_all_data()
        self.generate_report()
        self.analyze_price_trends()
        self.analyze_fundamentals()
        logger.info("Analysis complete!")

if __name__ == '__main__':
    analyzer = DataAnalyzer()
    analyzer.run()

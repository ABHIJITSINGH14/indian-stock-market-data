#!/usr/bin/env python
"""
Main orchestration script to run all data downloads
"""

import sys
import json
import os
from pathlib import Path
from config.config import LOG_DIR
import time
from datetime import datetime
from scripts.download_nse_data import NSEDataDownloader
from scripts.download_bse_data import BSEDataDownloader
from scripts.download_fundamentals import FundamentalsDownloader
from scripts.bulk_block_deals import BulkBlockDealsDownloader
from scripts.corporate_actions import CorporateActionsDownloader
from utils.logger import setup_logger

logger = setup_logger(__name__)

def run_all_downloads():
    """
    Execute all download tasks sequentially
    """
    logger.info("="*80)
    logger.info(f"Starting comprehensive data download at {datetime.now()}")
    logger.info("="*80)
    
    tasks = [
        ("NSE Historical Data", NSEDataDownloader(continue_after_row_rejection=True, defer_archival_prices=True)),
        ("BSE Company Data", BSEDataDownloader()),
        ("Company Fundamentals", FundamentalsDownloader()),
        ("Bulk & Block Deals", BulkBlockDealsDownloader()),
        ("Corporate Actions", CorporateActionsDownloader()),
    ]
    
    results = {}
    
    for task_name, downloader in tasks:
        try:
            logger.info(f"\n{'='*80}")
            logger.info(f"Starting: {task_name}")
            logger.info(f"{'='*80}")
            
            start_time = time.time()
            success = downloader.run()
            elapsed_time = time.time() - start_time
            
            results[task_name] = {
                'status': 'SUCCESS' if success else 'FAILED',
                'time': elapsed_time
            }
            
            logger.info(f"{task_name}: {'SUCCESS' if success else 'FAILED'} (Time: {elapsed_time:.2f}s)")
            
            # Add delay between tasks to avoid rate limiting
            time.sleep(2)
        
        except Exception as e:
            logger.error(f"Error during {task_name}: {str(e)}")
            results[task_name] = {
                'status': 'ERROR',
                'time': 0,
                'error': str(e)
            }
    
    # Keep returned/missing identity coverage alongside success/failure diagnostics.
    for task_name, downloader in tasks:
        coverage = getattr(downloader, 'coverage', None)
        if isinstance(coverage, dict):
            results[task_name]['coverage'] = coverage

    # Print summary
    logger.info(f"\n{'='*80}")
    logger.info("DOWNLOAD SUMMARY")
    logger.info(f"{'='*80}")
    
    total_time = sum(r.get('time', 0) for r in results.values())
    successful = sum(1 for r in results.values() if r['status'] == 'SUCCESS')
    failed = sum(1 for r in results.values() if r['status'] != 'SUCCESS')
    
    for task_name, result in results.items():
        status_symbol = '✓' if result['status'] == 'SUCCESS' else '✗'
        logger.info(f"{status_symbol} {task_name}: {result['status']} ({result.get('time', 0):.2f}s)")
    
    logger.info(f"\nTotal: {successful} successful, {failed} failed")
    logger.info(f"Total time: {total_time:.2f} seconds")
    logger.info(f"Completed at: {datetime.now()}")
    logger.info(f"{'='*80}\n")
    
    # Diagnostics are separate from data/raw: a manifest is not a dataset.
    summary = {
        'schema_version': 1,
        'run_id': os.environ.get('GITHUB_RUN_ID'),
        'commit_sha': os.environ.get('GITHUB_SHA'),
        'all_tasks_success': failed == 0,
        'historical_completeness': 'not_verified',
        'tasks': results,
    }
    Path(LOG_DIR, 'download-summary.json').write_text(
        json.dumps(summary, indent=2, allow_nan=False) + '\n', encoding='utf-8'
    )
    return failed == 0

if __name__ == '__main__':
    success = run_all_downloads()
    sys.exit(0 if success else 1)

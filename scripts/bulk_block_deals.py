from datetime import datetime, timedelta
from io import StringIO

import pandas as pd
import requests

from config.config import BULK_DEALS_CSV_PATH, BLOCK_DEALS_CSV_PATH
from utils.logger import setup_logger
from utils.api_client import APIClient
from utils.data_processor import DataProcessor

logger = setup_logger(__name__)


class BulkBlockDealsDownloader:
    """Legacy NSE collector. Transport failure is never evidence of no deals.

    The legacy endpoint is intentionally not replaced by an unverified URL.
    Partial observations can be saved, but cannot make an incomplete run succeed.
    """

    def __init__(self):
        self.client = APIClient()
        self.processor = DataProcessor()
        self.bulk_deals = pd.DataFrame()
        self.block_deals = pd.DataFrame()
        self.source_blocked = False

    def _fetch_deals(self, kind, start_date, end_date):
        if kind not in ('bulk', 'block'):
            raise ValueError('Unknown deal kind')
        if start_date > end_date:
            raise ValueError('start_date must not exceed end_date')
        attr = f'{kind}_deals'
        setattr(self, attr, pd.DataFrame())
        if self.source_blocked:
            logger.error("%s deals not attempted: source failed earlier in this run", kind)
            return False
        frames = []
        complete = True
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime('%d%b%Y')
            url = f'https://www1.nseindia.com/mweb/reports/{kind}deals_{date_str}.csv'
            try:
                response = self.client.get(url, retry=False)
                if response is None:
                    raise ValueError('No response received')
                frame = pd.read_csv(StringIO(response.text))
                columns = {str(column).strip().lower() for column in frame.columns}
                if not {'date', 'symbol'}.issubset(columns):
                    raise ValueError('Response is not a verified deals CSV (date/symbol missing)')
                frames.append(frame)
            except requests.exceptions.HTTPError as exc:
                complete = False
                status = exc.response.status_code if exc.response is not None else None
                logger.error("Unverified %s deals for %s: HTTP %s", kind, date_str, status)
                # A missing daily resource is unknown, not a confirmed no-deal day.
                if status != 404:
                    self.source_blocked = True
                    break
            except requests.exceptions.RequestException as exc:
                complete = False
                self.source_blocked = True
                logger.error("%s source unavailable on %s: %s", kind, date_str, exc)
                break
            except (ValueError, pd.errors.ParserError) as exc:
                complete = False
                self.source_blocked = True
                logger.error("%s source payload rejected on %s: %s", kind, date_str, exc)
                break
            current_date += timedelta(days=1)
        if frames:
            setattr(self, attr, pd.concat(frames, ignore_index=True))
        # Empty/unknown windows need a separate no-deals evidence contract.
        return complete and not getattr(self, attr).empty

    def fetch_bulk_deals(self, start_date, end_date):
        return self._fetch_deals('bulk', start_date, end_date)

    def fetch_block_deals(self, start_date, end_date):
        return self._fetch_deals('block', start_date, end_date)

    def save_data(self):
        outcomes = []
        for frame, path in (
            (self.bulk_deals, BULK_DEALS_CSV_PATH),
            (self.block_deals, BLOCK_DEALS_CSV_PATH),
        ):
            if not frame.empty:
                outcomes.append(self.processor.save_csv(self.processor.clean_data(frame), path))
        return bool(outcomes) and all(outcomes)

    def run(self):
        logger.info("Starting bulk and block deals download...")
        self.source_blocked = False
        self.bulk_deals = pd.DataFrame()
        self.block_deals = pd.DataFrame()
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=90)
        bulk_ok = self.fetch_bulk_deals(start_date, end_date)
        block_ok = self.fetch_block_deals(start_date, end_date)
        saved_ok = self.save_data()  # Preserve any partial observations for diagnosis.
        success = bulk_ok and block_ok and saved_ok
        if not success:
            logger.error("Bulk/block collection incomplete; partial files are not complete coverage")
        return success


if __name__ == '__main__':
    import sys
    sys.exit(0 if BulkBlockDealsDownloader().run() else 1)

"""Resilient orchestration for official master and daily-price ingestion."""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from market_data.database import MarketDatabase


logger = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    source: str
    dataset: str
    attempted: int
    succeeded: int
    failed: int
    rows_written: int

    @property
    def success(self) -> bool:
        return self.failed == 0 and self.succeeded > 0


def weekdays(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


class IngestionService:
    def __init__(self, database: MarketDatabase):
        self.database = database

    def ingest_master(self, source) -> IngestionResult:
        dataset = "security_master"
        run_id = self.database.start_run(source.name, dataset)
        try:
            records = source.fetch_master()
            written = self.database.upsert_securities(records)
            self.database.record_checkpoint(
                source.name, dataset, "latest", "success", row_count=written
            )
            result = IngestionResult(source.name, dataset, 1, 1, 0, written)
        except Exception as exc:
            logger.exception("%s security master ingestion failed", source.name.upper())
            self.database.record_error(run_id, source.name, dataset, "latest", exc)
            self.database.record_checkpoint(
                source.name, dataset, "latest", "failed", error=str(exc)
            )
            result = IngestionResult(source.name, dataset, 1, 0, 1, 0)
        self.database.finish_run(
            run_id,
            "success" if result.success else "failed",
            result.attempted,
            result.succeeded,
            result.failed,
            result.rows_written,
        )
        return result

    def ingest_prices(
        self,
        source,
        start: date,
        end: date,
        retry_failed: bool = False,
    ) -> IngestionResult:
        dataset = "daily_prices"
        run_id = self.database.start_run(source.name, dataset)
        successful = self.database.successful_checkpoint_dates(
            source.name, dataset, start, end
        )
        requested = set(weekdays(start, end))
        if retry_failed:
            requested.update(
                self.database.failed_checkpoint_dates(source.name, dataset, end)
            )
        pending = sorted(requested - successful)
        succeeded = failed = rows_written = 0
        for trading_date in pending:
            key = trading_date.isoformat()
            try:
                records = source.fetch_bhavcopy(trading_date)
                written = self.database.upsert_prices(records)
                self.database.record_checkpoint(
                    source.name,
                    dataset,
                    key,
                    "success",
                    checkpoint_date=trading_date,
                    row_count=written,
                )
                rows_written += written
                succeeded += 1
            except Exception as exc:
                logger.error(
                    "%s bhavcopy failed for %s: %s",
                    source.name.upper(),
                    trading_date,
                    exc,
                )
                self.database.record_error(
                    run_id, source.name, dataset, key, exc
                )
                self.database.record_checkpoint(
                    source.name,
                    dataset,
                    key,
                    "failed",
                    checkpoint_date=trading_date,
                    error=str(exc),
                )
                failed += 1
        result = IngestionResult(
            source.name, dataset, len(pending), succeeded, failed, rows_written
        )
        if not pending:
            status = "success"
        elif failed == 0:
            status = "success"
        elif succeeded:
            status = "partial"
        else:
            status = "failed"
        self.database.finish_run(
            run_id,
            status,
            result.attempted,
            result.succeeded,
            result.failed,
            result.rows_written,
            "No pending dates" if not pending else "",
        )
        return result

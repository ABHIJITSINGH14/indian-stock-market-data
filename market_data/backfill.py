"""Bounded-concurrency historical archive backfill with serialized DB writes."""

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from market_data.calendar import is_trading_day, trading_days
from market_data.database import MarketDatabase


logger = logging.getLogger(__name__)
PUBLIC_BOUNDARIES = {"nse": date(1994, 11, 3), "bse": date(2006, 3, 1)}
RECENT_GAP_DAYS = 10


@dataclass
class DownloadResult:
    source: str
    trading_date: date
    status: str
    records: Sequence[Dict[str, object]]
    error: Optional[Exception] = None


@dataclass
class BackfillResult:
    attempted: int = 0
    succeeded: int = 0
    unavailable: int = 0
    failed: int = 0
    rows_written: int = 0
    interrupted: bool = False


def classify_download_error(error: Exception, trading_date: date, end: date) -> str:
    text = str(error).lower()
    transient = (
        "403" in text
        or "429" in text
        or any(" {} ".format(code) in " {} ".format(text) for code in range(500, 600))
        or "timeout" in text
        or "connection" in text
        or "circuit open" in text
    )
    unavailable = (
        "404" in text
        or "not found" in text
    )
    if (
        unavailable
        and not transient
        and trading_date < end - timedelta(days=RECENT_GAP_DAYS)
    ):
        return "not_published"
    return "failed"


class ConcurrentArchiveFetcher:
    """Give each worker its own source/client while sharing host throttling."""

    def __init__(self, source_factory: Callable[[str], object]):
        self.source_factory = source_factory
        self.local = threading.local()
        self._clients = []
        self._lock = threading.Lock()

    def __call__(self, source: str, trading_date: date):
        sources = getattr(self.local, "sources", None)
        if sources is None:
            sources = {}
            self.local.sources = sources
        if source not in sources:
            source_object = self.source_factory(source)
            sources[source] = source_object
            with self._lock:
                self._clients.append(source_object.client)
        return sources[source].fetch_bhavcopy(trading_date)

    def close(self) -> None:
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            client.close()


class HistoricalBackfill:
    def __init__(
        self,
        database: MarketDatabase,
        fetch: Callable[[str, date], Sequence[Dict[str, object]]],
        workers: int = 4,
        progress_interval: float = 30.0,
    ):
        if workers < 1 or workers > 16:
            raise ValueError("workers must be between 1 and 16")
        self.database = database
        self.fetch = fetch
        self.workers = workers
        self.progress_interval = progress_interval

    def plan(
        self,
        sources: Iterable[str],
        starts: Dict[str, date],
        end: date,
        only_failures: bool = False,
    ) -> List[Tuple[str, date]]:
        planned = []
        for source in sources:
            start = max(starts[source], PUBLIC_BOUNDARIES[source])
            if start > end:
                continue
            unavailable_months = self.database.unavailable_months(source)
            if only_failures:
                dates = self.database.checkpoint_dates(
                    source, "daily_prices", start, end, ("failed",)
                )
            else:
                terminal = self.database.checkpoint_dates(
                    source,
                    "daily_prices",
                    start,
                    end,
                    ("success", "holiday", "not_published"),
                )
                dates = {
                    item
                    for item in trading_days(start, end)
                    if item.strftime("%Y-%m") not in unavailable_months
                } - terminal
            planned.extend((source, item) for item in sorted(dates))
        return sorted(planned, key=lambda item: (item[1], item[0]))

    def run(
        self,
        plan: Sequence[Tuple[str, date]],
        end: date,
        dry_run: bool = False,
    ) -> BackfillResult:
        result = BackfillResult(attempted=len(plan) if dry_run else 0)
        if dry_run or not plan:
            return result
        run_ids = {
            source: self.database.start_run(source, "daily_prices")
            for source in sorted({item[0] for item in plan})
        }
        started = time.monotonic()
        last_log = started
        futures = {}
        iterator = iter(plan)
        disabled_sources = set()
        blocked_counts = {source: 0 for source in run_ids}
        fatal_error = None
        source_stats = {
            source: {"attempted": 0, "succeeded": 0, "failed": 0, "rows": 0}
            for source in run_ids
        }
        executor = ThreadPoolExecutor(max_workers=self.workers)

        def submit_one() -> bool:
            while True:
                try:
                    source, trading_date = next(iterator)
                except StopIteration:
                    return False
                if source not in disabled_sources:
                    break
            future = executor.submit(self._download, source, trading_date, end)
            futures[future] = (source, trading_date)
            result.attempted += 1
            source_stats[source]["attempted"] += 1
            return True

        try:
            for _ in range(min(len(plan), self.workers * 2)):
                submit_one()
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future)
                    item = future.result()
                    self._write(run_ids[item.source], item, result)
                    if item.status == "success":
                        source_stats[item.source]["succeeded"] += 1
                        source_stats[item.source]["rows"] += len(item.records)
                    elif item.status == "failed":
                        source_stats[item.source]["failed"] += 1
                    error_text = str(item.error).lower() if item.error else ""
                    if item.status == "failed" and (
                        "403" in error_text or "circuit open" in error_text
                    ):
                        blocked_counts[item.source] += 1
                        if blocked_counts[item.source] >= self.workers:
                            disabled_sources.add(item.source)
                            logger.error(
                                "%s archive host appears rate-blocked; pausing this "
                                "exchange until a later resume",
                                item.source.upper(),
                            )
                    elif item.status == "success":
                        blocked_counts[item.source] = 0
                    submit_one()
                now = time.monotonic()
                if now - last_log >= self.progress_interval:
                    complete = result.succeeded + result.unavailable + result.failed
                    rate = complete / max(now - started, 0.001)
                    remaining = len(plan) - complete
                    logger.info(
                        "backfill progress %d/%d (%.1f%%), rows=%d, failures=%d, ETA=%s",
                        complete,
                        len(plan),
                        100.0 * complete / len(plan),
                        result.rows_written,
                        result.failed,
                        self._eta(remaining, rate),
                    )
                    last_log = now
        except KeyboardInterrupt:
            result.interrupted = True
            for future in futures:
                future.cancel()
        except BaseException as error:
            fatal_error = error
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            for source, run_id in run_ids.items():
                stats = source_stats[source]
                self.database.finish_run(
                    run_id,
                    "interrupted" if result.interrupted else (
                        "failed"
                        if fatal_error or stats["failed"] or source in disabled_sources
                        else "success"
                    ),
                    stats["attempted"],
                    stats["succeeded"],
                    stats["failed"],
                    stats["rows"],
                    "Host circuit opened; resume after cooldown"
                    if source in disabled_sources
                    else str(fatal_error) if fatal_error else "",
                )
            for source, year_month in {
                (source, trading_date.strftime("%Y-%m"))
                for source, trading_date in plan
            }:
                self.database.refresh_month_availability(source, year_month)
        return result

    def _download(self, source: str, trading_date: date, end: date) -> DownloadResult:
        try:
            records = self.fetch(source, trading_date)
            return DownloadResult(source, trading_date, "success", records)
        except Exception as error:
            return DownloadResult(
                source,
                trading_date,
                classify_download_error(error, trading_date, end),
                (),
                error,
            )

    def _write(
        self, run_id: int, item: DownloadResult, result: BackfillResult
    ) -> None:
        key = item.trading_date.isoformat()
        if item.status == "success":
            written = self.database.upsert_prices(item.records)
            self.database.record_checkpoint(
                item.source,
                "daily_prices",
                key,
                "success",
                checkpoint_date=item.trading_date,
                row_count=written,
            )
            result.succeeded += 1
            result.rows_written += written
            return
        self.database.record_checkpoint(
            item.source,
            "daily_prices",
            key,
            item.status,
            checkpoint_date=item.trading_date,
            error=str(item.error),
        )
        if item.status == "not_published":
            result.unavailable += 1
            logger.debug("%s %s archive not published", item.source.upper(), key)
        else:
            result.failed += 1
            self.database.record_error(
                run_id, item.source, "daily_prices", key, item.error or RuntimeError(key)
            )
            logger.error("%s %s failed: %s", item.source.upper(), key, item.error)

    @staticmethod
    def _eta(remaining: int, rate: float) -> str:
        if rate <= 0:
            return "unknown"
        seconds = int(remaining / rate)
        return "{}h{:02d}m".format(seconds // 3600, seconds % 3600 // 60)


def coverage_report(
    database: MarketDatabase,
    sources: Iterable[str],
    starts: Dict[str, date],
    end: date,
) -> List[Dict[str, object]]:
    reports = []
    for source in sources:
        start = max(starts[source], PUBLIC_BOUNDARIES[source])
        snapshot = database.coverage_snapshot(source, start, end)
        expected = set(trading_days(start, end))
        present = snapshot.pop("present_dates")
        missing = sorted(expected - present)
        snapshot.update(
            expected_days=len(expected),
            missing_days=len(missing),
            missing_dates=missing,
        )
        reports.append(snapshot)
    return reports

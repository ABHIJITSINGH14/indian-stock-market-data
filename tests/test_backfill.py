import threading
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import func, select

from market_data.backfill import (
    HistoricalBackfill,
    PUBLIC_BOUNDARIES,
    classify_download_error,
    coverage_report,
)
from market_data.calendar import IST, is_trading_day, last_completed_trading_day
from market_data.database import MarketDatabase, daily_prices, ingestion_runs
from market_data.http import HostCircuitOpen, HostRateLimiter
from scripts.backfill import build_parser


def price(source, trading_date, symbol="ABC"):
    return {
        "exchange": source.upper(),
        "symbol": symbol,
        "series": "EQ" if source == "nse" else "A",
        "scrip_code": "" if source == "nse" else "500001",
        "trading_date": trading_date,
        "close": 10.0,
        "source": "fixture",
    }


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        path = Path(self.temp.name) / "market.db"
        self.database = MarketDatabase("sqlite:///{}".format(path))
        self.database.initialize()

    def tearDown(self):
        self.database.engine.dispose()
        self.temp.cleanup()

    def test_cli_defaults_use_verified_boundaries(self):
        args = build_parser().parse_args(["backfill"])
        self.assertEqual(args.nse_start, PUBLIC_BOUNDARIES["nse"])
        self.assertEqual(args.bse_start, PUBLIC_BOUNDARIES["bse"])
        self.assertEqual(args.workers, 4)

    def test_calendar_skips_weekends_and_reliable_holidays(self):
        self.assertFalse(is_trading_day(date(2026, 9, 19)))
        self.assertFalse(is_trading_day(date(2026, 10, 2)))
        before_open = datetime(2026, 9, 18, 7, tzinfo=IST)
        self.assertEqual(last_completed_trading_day(before_open), date(2026, 9, 17))

    def test_old_not_found_is_unavailable_but_recent_and_403_fail(self):
        end = date(2026, 9, 17)
        self.assertEqual(
            classify_download_error(RuntimeError("HTTP 404"), date(2000, 1, 3), end),
            "not_published",
        )
        self.assertEqual(
            classify_download_error(RuntimeError("HTTP 404"), date(2026, 9, 16), end),
            "failed",
        )
        self.assertEqual(
            classify_download_error(RuntimeError("HTTP 403"), date(2000, 1, 3), end),
            "failed",
        )
        self.assertEqual(
            classify_download_error(
                RuntimeError("UDiFF: HTTP 404; legacy: HTTP 403"),
                date(2000, 1, 3),
                end,
            ),
            "failed",
        )
        self.assertEqual(
            classify_download_error(
                RuntimeError("Official source returned HTML instead of market data"),
                date(2000, 1, 3),
                end,
            ),
            "failed",
        )

    def test_concurrent_fetches_use_serialized_writer_and_are_idempotent(self):
        fetch_threads = set()
        write_threads = set()
        original = self.database.upsert_prices

        def fetch(source, trading_date):
            fetch_threads.add(threading.get_ident())
            time.sleep(0.01)
            return [price(source, trading_date)]

        def tracked_write(records):
            write_threads.add(threading.get_ident())
            return original(records)

        self.database.upsert_prices = tracked_write
        service = HistoricalBackfill(self.database, fetch, workers=3, progress_interval=99)
        plan = [("nse", date(2024, 1, day)) for day in (2, 3, 4, 5)]
        first = service.run(plan, date(2024, 1, 5))
        second = service.run(plan, date(2024, 1, 5))
        self.assertGreater(len(fetch_threads), 1)
        self.assertEqual(write_threads, {threading.get_ident()})
        self.assertEqual(first.rows_written, 4)
        self.assertEqual(second.rows_written, 4)
        with self.database.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(daily_prices)), 4
            )

    def test_plan_resumes_and_retry_failed_targets_only_failures(self):
        starts = {"nse": date(2024, 1, 2)}
        self.database.record_checkpoint(
            "nse", "daily_prices", "2024-01-02", "success", date(2024, 1, 2), 1
        )
        self.database.record_checkpoint(
            "nse", "daily_prices", "2024-01-03", "failed", date(2024, 1, 3), 0, "x"
        )
        service = HistoricalBackfill(self.database, lambda source, day: [])
        resumed = service.plan(["nse"], starts, date(2024, 1, 4))
        failures = service.plan(
            ["nse"], starts, date(2024, 1, 4), only_failures=True
        )
        self.assertEqual(resumed, [("nse", date(2024, 1, 3)), ("nse", date(2024, 1, 4))])
        self.assertEqual(failures, [("nse", date(2024, 1, 3))])

    def test_interruption_leaves_unwritten_dates_for_resume(self):
        calls = 0

        def interrupted(source, trading_date):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()
            return [price(source, trading_date)]

        service = HistoricalBackfill(self.database, interrupted, workers=1)
        plan = [("nse", date(2024, 1, 2)), ("nse", date(2024, 1, 3))]
        result = service.run(plan, date(2024, 1, 3))
        self.assertTrue(result.interrupted)
        resumed = service.plan(
            ["nse"], {"nse": date(2024, 1, 2)}, date(2024, 1, 3)
        )
        self.assertEqual(resumed, plan)

    def test_coverage_reports_rows_statuses_and_missing_dates(self):
        self.database.upsert_prices([price("nse", date(2024, 1, 2))])
        self.database.record_checkpoint(
            "nse", "daily_prices", "2024-01-02", "success", date(2024, 1, 2), 1
        )
        report = coverage_report(
            self.database,
            ["nse"],
            {"nse": date(2024, 1, 2)},
            date(2024, 1, 4),
        )[0]
        self.assertEqual(report["rows"], 1)
        self.assertEqual(report["present_days"], 1)
        self.assertEqual(report["missing_dates"], [date(2024, 1, 3), date(2024, 1, 4)])
        self.assertEqual(report["status_counts"], {"success": 1})

    def test_rate_limiter_opens_circuit_after_repeated_403(self):
        limiter = HostRateLimiter(0, failure_threshold=2, cooldown=60)
        url = "https://archives.nseindia.com/file.zip"
        limiter.response(url, 403)
        limiter.response(url, 403)
        with self.assertRaises(HostCircuitOpen):
            limiter.wait(url)

    def test_run_status_is_per_exchange(self):
        def fetch(source, trading_date):
            if source == "nse":
                raise RuntimeError("temporary source failure")
            return [price(source, trading_date)]

        service = HistoricalBackfill(self.database, fetch, workers=2)
        service.run(
            [("nse", date(2024, 1, 2)), ("bse", date(2024, 1, 2))],
            date(2024, 1, 2),
        )
        with self.database.engine.connect() as connection:
            statuses = dict(
                connection.execute(
                    select(ingestion_runs.c.source, ingestion_runs.c.status)
                ).all()
            )
        self.assertEqual(statuses, {"nse": "failed", "bse": "success"})

    def test_database_exception_marks_run_failed_before_reraising(self):
        self.database.upsert_prices = lambda records: (_ for _ in ()).throw(
            RuntimeError("disk full")
        )
        service = HistoricalBackfill(
            self.database,
            lambda source, trading_date: [price(source, trading_date)],
            workers=1,
        )
        with self.assertRaisesRegex(RuntimeError, "disk full"):
            service.run([("nse", date(2024, 1, 2))], date(2024, 1, 2))
        with self.database.engine.connect() as connection:
            status = connection.scalar(select(ingestion_runs.c.status))
        self.assertEqual(status, "failed")


if __name__ == "__main__":
    unittest.main()

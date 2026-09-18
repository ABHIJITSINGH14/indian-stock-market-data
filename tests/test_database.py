import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from market_data.database import (
    MarketDatabase,
    daily_prices,
    exchange_symbols,
    schema_migrations,
    securities,
)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        path = Path(self.temp_dir.name) / "market.db"
        self.database = MarketDatabase("sqlite:///{}".format(path))
        self.database.initialize()

    def tearDown(self):
        self.database.engine.dispose()
        self.temp_dir.cleanup()

    def test_schema_initialization_and_cross_exchange_upsert_are_idempotent(self):
        records = [
            {
                "exchange": "NSE",
                "symbol": "RELIANCE",
                "series": "EQ",
                "name": "Reliance Industries",
                "isin": "INE002A01018",
                "source": "fixture",
            },
            {
                "exchange": "BSE",
                "symbol": "RELIANCE",
                "series": "A",
                "scrip_code": "500325",
                "name": "Reliance Industries Ltd",
                "isin": "INE002A01018",
                "source": "fixture",
            },
        ]
        self.database.upsert_securities(records)
        self.database.upsert_securities(records)
        with self.database.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(securities)), 1
            )
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(exchange_symbols)), 2
            )
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(schema_migrations)), 1
            )

    def test_concurrent_schema_initialization_is_serialized(self):
        path = Path(self.temp_dir.name) / "concurrent.db"
        databases = [
            MarketDatabase("sqlite:///{}".format(path))
            for _ in range(8)
        ]
        try:
            with ThreadPoolExecutor(max_workers=len(databases)) as executor:
                list(executor.map(lambda database: database.initialize(), databases))
            with databases[0].engine.connect() as connection:
                self.assertEqual(
                    connection.scalar(
                        select(func.count()).select_from(schema_migrations)
                    ),
                    1,
                )
        finally:
            for database in databases:
                database.engine.dispose()

    def test_price_resolution_is_atomic_with_concurrent_security_merge(self):
        path = Path(self.temp_dir.name) / "merge-race.db"
        price_database = MarketDatabase("sqlite:///{}".format(path))
        merge_database = MarketDatabase("sqlite:///{}".format(path))
        price_database.initialize()
        price_database.upsert_securities(
            [
                {
                    "exchange": "NSE",
                    "symbol": "ABC",
                    "series": "EQ",
                    "name": "ABC Limited",
                    "source": "fixture",
                }
            ]
        )
        resolved = threading.Event()
        release = threading.Event()
        original_resolve = price_database._resolve_security

        def paused_resolve(connection, record):
            security_id = original_resolve(connection, record)
            resolved.set()
            self.assertTrue(release.wait(2))
            return security_id

        price_database._resolve_security = paused_resolve
        price = {
            "exchange": "NSE",
            "symbol": "ABC",
            "series": "EQ",
            "trading_date": date(2024, 1, 2),
            "close": 10.0,
            "source": "fixture",
        }
        canonical = {
            "exchange": "NSE",
            "symbol": "ABC",
            "series": "EQ",
            "name": "ABC Limited",
            "isin": "INE123A01010",
            "source": "fixture",
        }
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                price_future = executor.submit(price_database.upsert_prices, [price])
                self.assertTrue(resolved.wait(2))
                merge_future = executor.submit(
                    merge_database.upsert_securities, [canonical]
                )
                self.assertFalse(merge_future.done())
                release.set()
                self.assertEqual(price_future.result(timeout=5), 1)
                self.assertEqual(merge_future.result(timeout=5), 1)
            with price_database.engine.connect() as connection:
                self.assertEqual(
                    connection.scalar(select(func.count()).select_from(daily_prices)), 1
                )
        finally:
            release.set()
            price_database.engine.dispose()
            merge_database.engine.dispose()

    def test_price_upsert_resolves_bse_scrip_code_and_updates_in_place(self):
        self.database.upsert_securities(
            [
                {
                    "exchange": "BSE",
                    "symbol": "RELIANCE",
                    "series": "A",
                    "scrip_code": "500325",
                    "name": "Reliance Industries Ltd",
                    "isin": "INE002A01018",
                    "source": "fixture",
                }
            ]
        )
        price = {
            "exchange": "BSE",
            "symbol": "RELIANCE INDUSTRIES",
            "series": "A",
            "scrip_code": "500325",
            "trading_date": date(2024, 1, 2),
            "close": 100.0,
            "volume": 10,
            "source": "fixture",
        }
        self.database.upsert_prices([price])
        price["close"] = 101.5
        self.database.upsert_prices([price])
        with self.database.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(daily_prices)), 1
            )
            self.assertEqual(connection.scalar(select(daily_prices.c.close)), 101.5)

    def test_master_merges_price_fallback_identity_into_isin_identity(self):
        price = {
            "exchange": "NSE",
            "symbol": "ABC",
            "series": "EQ",
            "trading_date": date(2024, 1, 2),
            "close": 10.0,
            "source": "fixture",
        }
        self.database.upsert_prices([price])
        self.database.upsert_securities(
            [
                {
                    "exchange": "NSE",
                    "symbol": "ABC",
                    "series": "EQ",
                    "name": "ABC Limited",
                    "isin": "INE123A01010",
                    "source": "fixture",
                },
                {
                    "exchange": "BSE",
                    "symbol": "ABC",
                    "series": "A",
                    "scrip_code": "500001",
                    "name": "ABC Limited",
                    "isin": "INE123A01010",
                    "source": "fixture",
                },
            ]
        )
        with self.database.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(securities)), 1
            )
            canonical = connection.scalar(select(securities.c.canonical_id))
            self.assertEqual(canonical, "isin:INE123A01010")
            price_security = connection.scalar(select(daily_prices.c.security_id))
            security = connection.scalar(select(securities.c.id))
            self.assertEqual(price_security, security)

    def test_late_isin_merges_bse_fallback_into_existing_nse_security(self):
        self.database.upsert_securities(
            [{
                "exchange": "NSE", "symbol": "RELIANCE", "series": "EQ",
                "isin": "INE002A01018", "name": "Reliance Industries Limited",
                "source": "nse_master",
            }]
        )
        self.database.upsert_securities(
            [{
                "exchange": "BSE", "symbol": "RELIANCE", "series": "A",
                "scrip_code": "500325", "name": "RELIANCE",
                "source": "bse_disclosure",
            }]
        )
        self.database.upsert_prices(
            [{
                "exchange": "BSE", "symbol": "RELIANCE", "series": "A",
                "scrip_code": "500325", "isin": "INE002A01018",
                "name": "Reliance Industries Limited",
                "trading_date": date(2026, 9, 17), "close": 1240.8,
                "source": "bse_bhavcopy",
            }]
        )
        with self.database.engine.connect() as connection:
            mapped_ids = set(
                connection.execute(
                    select(exchange_symbols.c.security_id).where(
                        exchange_symbols.c.normalized_symbol == "RELIANCE"
                    )
                ).scalars()
            )
            isin_count = connection.scalar(
                select(func.count()).select_from(securities).where(
                    securities.c.isin == "INE002A01018"
                )
            )
            canonical_name = connection.scalar(
                select(securities.c.name).where(
                    securities.c.isin == "INE002A01018"
                )
            )
        self.assertEqual(isin_count, 1)
        self.assertEqual(len(mapped_ids), 1)
        self.assertEqual(canonical_name, "Reliance Industries Limited")

    def test_bse_symbol_change_resolves_latest_active_scrip_mapping(self):
        self.database.upsert_securities(
            [{
                "exchange": "BSE", "symbol": "OLDNAME", "series": "A",
                "scrip_code": "500999", "name": "Example Limited",
                "source": "fixture",
            }]
        )
        self.database.upsert_securities(
            [{
                "exchange": "BSE", "symbol": "NEWNAME", "series": "A",
                "scrip_code": "500999", "name": "Example Limited",
                "source": "fixture",
            }]
        )
        self.database.upsert_prices(
            [{
                "exchange": "BSE", "symbol": "LATEST LABEL", "series": "A",
                "scrip_code": "500999", "trading_date": date(2026, 9, 17),
                "close": 10.0, "source": "fixture",
            }]
        )
        with self.database.engine.connect() as connection:
            active = connection.scalar(
                select(func.count()).select_from(exchange_symbols).where(
                    exchange_symbols.c.scrip_code == "500999",
                    exchange_symbols.c.active.is_(True),
                )
            )
        self.assertEqual(active, 1)

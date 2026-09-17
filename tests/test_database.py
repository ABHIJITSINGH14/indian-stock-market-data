import tempfile
import unittest
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

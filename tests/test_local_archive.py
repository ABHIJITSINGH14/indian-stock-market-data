import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from market_data.database import (
    MarketDatabase,
    daily_prices,
    ingestion_checkpoints,
    local_archive_assets,
    local_archive_imports,
)
from market_data.local_archive import (
    LocalArchiveError,
    LocalArchiveImporter,
    load_manifest,
)


BSE_CSV = (
    b"TradDt,FinInstrmId,ISIN,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,"
    b"ClsPric,LastPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd\n"
    b"2026-09-18,500002,INE117A01022,ABB,A,7175,7308.85,7162.90,"
    b"7235.20,7235.20,7129,18369,132648938,1685\n"
)


class LocalArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.raw_root = root / "raw"
        directory = self.raw_root / "bse_cm"
        directory.mkdir(parents=True)
        self.archive = (
            directory
            / "2026-09-18_BhavCopy_BSE_CM_0_0_0_20260918_F_0000.CSV"
        )
        self.archive.write_bytes(BSE_CSV)
        digest = hashlib.sha256(BSE_CSV).hexdigest()
        self.manifest = root / "manifest.jsonl"
        self.manifest.write_text(
            json.dumps(
                {
                    "kind": "bse_cm",
                    "tag": "2026-09-18",
                    "url": (
                        "https://www.bseindia.com/download/BhavCopy/Equity/"
                        "BhavCopy_BSE_CM_0_0_0_20260918_F_0000.CSV"
                    ),
                    "sha256": digest,
                    "bytes": len(BSE_CSV),
                    "fetched_utc": "2026-10-04T09:21:59+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.database = MarketDatabase("sqlite:///{}".format(root / "market.db"))

    def tearDown(self):
        self.database.engine.dispose()
        self.temp_dir.cleanup()

    def test_import_is_verified_resumable_and_records_provenance(self):
        entries = load_manifest(self.manifest, self.raw_root, ("bse_cm",))
        importer = LocalArchiveImporter(self.database, min_free_bytes=0)
        first = importer.import_entries(entries)
        second = importer.import_entries(entries)
        catalog = importer.catalog_entries(entries)
        self.assertEqual(first["imported_files"], 1)
        self.assertEqual(first["rows_written"], 1)
        self.assertEqual(second["skipped_imported"], 1)
        self.assertEqual(catalog["verified_assets"], 1)
        with self.database.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(daily_prices)), 1
            )
            self.assertEqual(
                connection.scalar(
                    select(func.count()).select_from(local_archive_imports)
                ),
                1,
            )
            self.assertEqual(
                connection.scalar(
                    select(local_archive_assets.c.status)
                ),
                "materialized",
            )
            checkpoint = connection.execute(
                select(
                    ingestion_checkpoints.c.status,
                    ingestion_checkpoints.c.checkpoint_date,
                )
            ).one()
        self.assertEqual(checkpoint, ("success", date(2026, 9, 18)))

    def test_existing_date_is_not_overwritten(self):
        self.database.initialize()
        self.database.upsert_prices(
            [
                {
                    "exchange": "BSE",
                    "symbol": "ABB",
                    "series": "A",
                    "scrip_code": "500002",
                    "trading_date": date(2026, 9, 18),
                    "close": 1.0,
                    "source": "existing",
                }
            ]
        )
        entries = load_manifest(self.manifest, self.raw_root, ("bse_cm",))
        result = LocalArchiveImporter(
            self.database, min_free_bytes=0
        ).import_entries(entries)
        self.assertEqual(result["skipped_present"], 1)
        with self.database.engine.connect() as connection:
            self.assertEqual(connection.scalar(select(daily_prices.c.close)), 1.0)

    def test_hash_mismatch_is_rejected(self):
        self.archive.write_bytes(BSE_CSV + b"tampered")
        entries = load_manifest(self.manifest, self.raw_root, ("bse_cm",))
        with self.assertRaisesRegex(LocalArchiveError, "size mismatch"):
            LocalArchiveImporter(
                self.database, min_free_bytes=0
            ).import_entries(entries)

    def test_catalog_only_verifies_without_materializing_prices(self):
        entries = load_manifest(self.manifest, self.raw_root, ("bse_cm",))
        result = LocalArchiveImporter(
            self.database, min_free_bytes=0
        ).catalog_entries(entries)
        self.assertEqual(result["verified_assets"], 1)
        with self.database.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(
                    select(func.count()).select_from(local_archive_assets)
                ),
                1,
            )
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(daily_prices)),
                0,
            )


if __name__ == "__main__":
    unittest.main()

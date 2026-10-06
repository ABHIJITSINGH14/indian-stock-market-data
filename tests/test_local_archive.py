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
    local_manifest_assets,
)
from market_data.local_archive import (
    LocalArchiveError,
    LocalArchiveImporter,
    load_external_manifest,
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

    def test_generic_catalog_supports_non_date_partition_tags(self):
        directory = self.raw_root / "api_ca"
        directory.mkdir()
        body = b"[]"
        archive = directory / "2026-01-01_2026-03-31.json"
        archive.write_bytes(body)
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "kind": "api_ca",
                        "tag": "2026-01-01_2026-03-31",
                        "url": (
                            "https://www.nseindia.com/api/corporates-corporateActions"
                            "?from=01-01-2026&to=31-03-2026"
                        ),
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "bytes": len(body),
                        "fetched_utc": "2026-10-06T05:00:00+00:00",
                    }
                )
                + "\n"
            )
        entries = load_external_manifest(self.manifest, self.raw_root)
        result = LocalArchiveImporter(
            self.database, min_free_bytes=0
        ).catalog_external_entries(entries)
        self.assertEqual(result["verified_manifest_assets"], 2)
        self.assertEqual(result["corrected_manifest_assets"], 0)
        self.assertEqual(result["missing_manifest_assets"], 0)
        self.assertEqual(result["quarantined_manifest_assets"], 0)
        with self.database.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(
                    select(func.count()).select_from(local_manifest_assets)
                ),
                2,
            )

    def test_quarantined_full_bhavcopy_is_admitted_with_observed_date(self):
        directory = self.raw_root / "nse_full_suspect"
        directory.mkdir()
        body = (
            b"SYMBOL, SERIES, DATE1, CLOSE_PRICE\n"
            b"ABC, EQ, 01-Oct-2019, 10.0\n"
            b"XYZ, EQ, 01-Oct-2019, 20.0\n"
        )
        name = "sec_bhavdata_full_02102019.csv"
        archive = directory / "2019-10-02_{}".format(name)
        archive.write_bytes(body)
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "kind": "nse_full",
                        "tag": "2019-10-02",
                        "url": "https://nsearchives.nseindia.com/products/content/"
                        + name,
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "bytes": len(body),
                        "fetched_utc": "2026-10-06T05:00:00+00:00",
                    }
                )
                + "\n"
            )
        entries = load_external_manifest(
            self.manifest, self.raw_root, ("nse_full",)
        )
        result = LocalArchiveImporter(
            self.database, min_free_bytes=0
        ).catalog_external_entries(entries)
        self.assertEqual(result["corrected_manifest_assets"], 1)
        self.assertEqual(result["quarantined_manifest_assets"], 0)
        with self.database.engine.connect() as connection:
            asset = connection.execute(
                select(
                    local_manifest_assets.c.status,
                    local_manifest_assets.c.observed_tag,
                    local_manifest_assets.c.correction_note,
                )
            ).one()
        self.assertEqual(asset.status, "corrected_external")
        self.assertEqual(asset.observed_tag, "2019-10-01")
        self.assertIn("2019-10-02", asset.correction_note)

    def test_generic_asset_compression_preserves_manifest_verification(self):
        directory = self.raw_root / "nse_mto"
        directory.mkdir()
        body = b"SYMBOL,SERIES,DELIV_QTY\nABC,EQ,12345\n" * 100
        name = "MTO_01012026.DAT"
        archive = directory / "2026-01-01_{}".format(name)
        archive.write_bytes(body)
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "kind": "nse_mto",
                        "tag": "2026-01-01",
                        "url": (
                            "https://nsearchives.nseindia.com/archives/equities/mto/"
                            + name
                        ),
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "bytes": len(body),
                        "fetched_utc": "2026-10-06T05:00:00+00:00",
                    }
                )
                + "\n"
            )
        importer = LocalArchiveImporter(self.database, min_free_bytes=0)
        entries = load_external_manifest(
            self.manifest, self.raw_root, ("nse_mto",)
        )
        result = importer.compress_external_entries(entries)
        self.assertEqual(result["compressed_manifest_assets"], 1)
        self.assertEqual(result["skipped_unverified_manifest_assets"], 0)
        self.assertFalse(archive.exists())
        self.assertTrue(Path(str(archive) + ".gz").exists())

        compressed = load_external_manifest(
            self.manifest, self.raw_root, ("nse_mto",)
        )
        catalog = importer.catalog_external_entries(compressed)
        self.assertEqual(catalog["verified_manifest_assets"], 1)
        with self.database.engine.connect() as connection:
            stored = connection.scalar(select(local_manifest_assets.c.local_path))
        self.assertTrue(stored.endswith(".gz"))


if __name__ == "__main__":
    unittest.main()

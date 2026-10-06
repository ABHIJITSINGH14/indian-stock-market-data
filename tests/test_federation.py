import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select

from market_data.database import (
    MarketDatabase,
    federated_catalogs,
    federated_dataset_bindings,
    federated_dataset_routes,
    federated_gap_records,
    federated_sources,
)
from market_data.federation import FederationCatalogError, import_sealed_catalog


CATALOG_SCHEMA = """
CREATE TABLE catalog_generation(
 generation_id TEXT, schema_version TEXT, inventory_generation_id TEXT,
 inventory_manifest_sha256 TEXT, inventory_gap_ledger_sha256 TEXT,
 source_policy_sha256 TEXT, route_policy_sha256 TEXT, builder_sha256 TEXT,
 cutoff_utc TEXT, created_at_utc TEXT, logical_digest TEXT, sealed INTEGER,
 research_admitted INTEGER, active_application_generation INTEGER
);
CREATE TABLE source(
 source_id TEXT, display_name TEXT, source_class TEXT, role TEXT,
 permission_state TEXT, normalization_state TEXT, predecessor_source_id TEXT,
 terms_fingerprint TEXT, constraint_text TEXT
);
CREATE TABLE dataset_binding(
 binding_id TEXT, source_id TEXT, dataset_key TEXT, exchange TEXT, segment TEXT,
 relation_locator TEXT, binding_format TEXT, disposition TEXT,
 physical_row_count INTEGER, event_min TEXT, event_max TEXT, knowledge_min TEXT,
 knowledge_max TEXT, identity_contract TEXT, knowledge_time_status TEXT,
 raw_lineage_status TEXT, research_admitted INTEGER,
 normalization_authorized INTEGER
);
CREATE TABLE dataset_route(
 route_id TEXT, dataset_key TEXT, exchange TEXT, segment TEXT, ordinal INTEGER,
 binding_id TEXT, action TEXT, scope_json TEXT, fallback_authorized INTEGER,
 rationale TEXT, rule_digest TEXT
);
CREATE TABLE gap_record(
 gap_id TEXT, dataset_key TEXT, exchange TEXT, segment TEXT, scope_kind TEXT,
 primary_class TEXT, secondary_classes_json TEXT, start_event TEXT,
 end_event TEXT, status TEXT, reason_code TEXT, source_id TEXT,
 record_json TEXT, evidence_json TEXT
);
"""


class FederationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.database = MarketDatabase("sqlite:///{}".format(root / "market.db"))
        self.catalog = root / "catalog.sqlite"
        self.manifest = root / "MANIFEST.json"
        self.generation_id = "a" * 64
        self._write_catalog()
        self._write_manifest()

    def tearDown(self):
        self.database.engine.dispose()
        self.temp_dir.cleanup()

    def _write_catalog(self, admitted=0):
        if self.catalog.exists():
            self.catalog.unlink()
        with sqlite3.connect(str(self.catalog)) as connection:
            connection.executescript(CATALOG_SCHEMA)
            connection.execute(
                "INSERT INTO catalog_generation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.generation_id,
                    "aaru.consolidated-catalog.v1",
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                    "e" * 64,
                    "f" * 64,
                    "1" * 64,
                    "2026-10-03T13:55:01Z",
                    "2026-10-03T13:55:01Z",
                    self.generation_id,
                    1,
                    admitted,
                    0,
                ),
            )
            connection.execute(
                "INSERT INTO source VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "source",
                    "Source",
                    "official_raw",
                    "provenance_sidecar",
                    "allowed",
                    "authorized",
                    None,
                    None,
                    "Read-only fixture",
                ),
            )
            connection.execute(
                "INSERT INTO dataset_binding VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "binding",
                    "source",
                    "cash_eod",
                    "NSE",
                    "cash",
                    "prices",
                    "sqlite_table",
                    "provenance_enrichment_only",
                    1,
                    "2024-01-01",
                    "2024-01-01",
                    None,
                    None,
                    "symbol+date",
                    "unknown",
                    "complete",
                    0,
                    1,
                ),
            )
            connection.execute(
                "INSERT INTO dataset_route VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "route",
                    "cash_eod",
                    "NSE",
                    "cash",
                    0,
                    "binding",
                    "provenance_enrichment_only",
                    "{}",
                    0,
                    "Fixture route",
                    "2" * 64,
                ),
            )
            connection.execute(
                "INSERT INTO gap_record VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "gap",
                    "cash_eod",
                    "BSE",
                    "cash",
                    "temporal",
                    "date_coverage",
                    "[]",
                    "2020-01-01",
                    "2020-01-02",
                    "open",
                    "missing",
                    "source",
                    "{}",
                    "{}",
                ),
            )

    def _write_manifest(self):
        digest = hashlib.sha256(self.catalog.read_bytes()).hexdigest()
        self.manifest.write_text(
            json.dumps(
                {
                    "generation_id": self.generation_id,
                    "outputs": {"catalog.sqlite": {"sha256": digest}},
                }
            ),
            encoding="utf-8",
        )

    def test_import_is_verified_and_idempotent(self):
        first = import_sealed_catalog(self.database, self.manifest)
        second = import_sealed_catalog(self.database, self.manifest)
        self.assertEqual(first, second)
        self.assertEqual(first["physical_payloads_copied"], 0)
        with self.database.engine.connect() as connection:
            for table in (
                federated_catalogs,
                federated_sources,
                federated_dataset_bindings,
                federated_dataset_routes,
                federated_gap_records,
            ):
                self.assertEqual(
                    connection.scalar(select(func.count()).select_from(table)), 1
                )

    def test_rejects_hash_mismatch(self):
        self.catalog.write_bytes(self.catalog.read_bytes() + b"tampered")
        with self.assertRaisesRegex(FederationCatalogError, "SHA-256"):
            import_sealed_catalog(self.database, self.manifest)

    def test_rejects_admitted_catalog(self):
        self._write_catalog(admitted=1)
        self._write_manifest()
        with self.assertRaisesRegex(FederationCatalogError, "non-admitted"):
            import_sealed_catalog(self.database, self.manifest)


if __name__ == "__main__":
    unittest.main()

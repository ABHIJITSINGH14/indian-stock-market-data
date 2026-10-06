"""Import sealed local data-lake catalogs without copying external payloads."""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Mapping

from market_data.database import (
    MarketDatabase,
    federated_catalogs,
    federated_dataset_bindings,
    federated_dataset_routes,
    federated_gap_records,
    federated_sources,
    utcnow,
)


class FederationCatalogError(ValueError):
    """Raised when an external catalog fails its integrity or safety contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_rows(connection: sqlite3.Connection, table: str) -> List[Dict[str, object]]:
    return [dict(row) for row in connection.execute("SELECT * FROM {}".format(table))]


def import_sealed_catalog(
    database: MarketDatabase, manifest_path: Path
) -> Mapping[str, object]:
    """Verify and register a sealed catalog as a read-only federation."""

    manifest_path = manifest_path.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    catalog_path = manifest_path.parent / "catalog.sqlite"
    if not catalog_path.is_file():
        raise FederationCatalogError("catalog.sqlite is missing beside the manifest")

    expected_hash = (
        manifest.get("outputs", {}).get("catalog.sqlite", {}).get("sha256")
    )
    observed_hash = _sha256(catalog_path)
    if not expected_hash or observed_hash != expected_hash:
        raise FederationCatalogError("catalog SHA-256 does not match the manifest")

    uri = "file:{}?mode=ro&immutable=1".format(catalog_path)
    with sqlite3.connect(uri, uri=True) as source:
        source.row_factory = sqlite3.Row
        generation_rows = _read_rows(source, "catalog_generation")
        if len(generation_rows) != 1:
            raise FederationCatalogError("catalog must contain exactly one generation")
        generation = generation_rows[0]
        generation_id = str(generation["generation_id"])
        if generation_id != manifest.get("generation_id"):
            raise FederationCatalogError("catalog generation does not match the manifest")
        if generation["logical_digest"] != generation_id:
            raise FederationCatalogError("catalog logical digest is inconsistent")
        if not generation["sealed"]:
            raise FederationCatalogError("catalog is not sealed")
        if generation["research_admitted"] or generation["active_application_generation"]:
            raise FederationCatalogError(
                "only non-admitted, inactive catalogs may be federated"
            )
        sources = _read_rows(source, "source")
        bindings = _read_rows(source, "dataset_binding")
        routes = _read_rows(source, "dataset_route")
        gaps = _read_rows(source, "gap_record")

    database.initialize()
    with database.transaction() as connection:
        for table in (
            federated_gap_records,
            federated_dataset_routes,
            federated_dataset_bindings,
            federated_sources,
        ):
            connection.execute(
                table.delete().where(table.c.generation_id == generation_id)
            )
        connection.execute(
            federated_catalogs.delete().where(
                federated_catalogs.c.generation_id == generation_id
            )
        )
        connection.execute(
            federated_catalogs.insert().values(
                generation_id=generation_id,
                schema_version=generation["schema_version"],
                catalog_path=str(catalog_path),
                manifest_path=str(manifest_path),
                catalog_sha256=observed_hash,
                cutoff_utc=generation["cutoff_utc"],
                sealed=True,
                research_admitted=False,
                active_application_generation=False,
                imported_at=utcnow(),
            )
        )
        if sources:
            connection.execute(
                federated_sources.insert(),
                [dict(row, generation_id=generation_id) for row in sources],
            )
        if bindings:
            connection.execute(
                federated_dataset_bindings.insert(),
                [dict(row, generation_id=generation_id) for row in bindings],
            )
        if routes:
            connection.execute(
                federated_dataset_routes.insert(),
                [dict(row, generation_id=generation_id) for row in routes],
            )
        if gaps:
            connection.execute(
                federated_gap_records.insert(),
                [dict(row, generation_id=generation_id) for row in gaps],
            )

    return {
        "generation_id": generation_id,
        "catalog_sha256": observed_hash,
        "sources": len(sources),
        "bindings": len(bindings),
        "routes": len(routes),
        "gaps": len(gaps),
        "physical_payloads_copied": 0,
    }

#!/usr/bin/env python3
"""Create a consistent AARU staging database from the official-source SQLite lake."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def consistent_backup(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(
        "file:{}?mode=ro".format(source),
        uri=True,
        timeout=60,
    )
    source_connection.execute("PRAGMA query_only=ON")
    source_connection.execute("PRAGMA busy_timeout=60000")
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection, pages=2048, sleep=0.05)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()


def ensure_integrity(connection: sqlite3.Connection) -> None:
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise RuntimeError("SQLite integrity failure: {}".format(integrity))
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            "Foreign-key violations in copied database: {}".format(violations[:20])
        )


def dataset_mappings() -> Sequence[Sequence[str]]:
    return (
        (
            "instrumentMaster",
            "securities+exchange_symbols",
            "instrumentMaster",
            "official_exchange",
            "Canonical identity still requires listing-lifecycle and instrument-class audit.",
        ),
        (
            "cashEOD",
            "daily_prices",
            "cashEOD",
            "official_exchange",
            "Official unadjusted cash-market observations.",
        ),
        (
            "exchangeFilings",
            "filings+raw_documents",
            "exchangeFilings",
            "official_exchange",
            "Revision-aware filing index and content-addressed document custody.",
        ),
        (
            "financialStatements",
            "financial_fact_instances",
            "financialStatements",
            "official_exchange",
            "Lossless XBRL fact instances; no aggregator override.",
        ),
        (
            "financialMetrics",
            "financial_metrics",
            "derived",
            "deterministic",
            "Reported and derived metrics remain explicitly distinguished.",
        ),
        (
            "shareholding",
            "shareholding_patterns",
            "shareholding",
            "official_exchange",
            "Point-in-time availability must be validated before research admission.",
        ),
        (
            "corporateActions",
            "corporate_actions",
            "corporateActions",
            "official_exchange",
            "Requires completeness and effective-date reconciliation.",
        ),
        (
            "insiderDisclosures",
            "pit_disclosures",
            "insiderDisclosures",
            "official_exchange",
            "PIT means prevention-of-insider-trading disclosure, not point-in-time certification.",
        ),
    )


def import_inventory(
    connection: sqlite3.Connection,
    snapshot_id: str,
    inventory: Mapping[str, Any],
) -> None:
    observed_at = str(inventory.get("observed_at") or utcnow())
    for row in inventory.get("price_coverage", []):
        if not row or row[0] == "query_error":
            continue
        exchange, _row_count, trading_dates, start, end, _securities = row
        connection.execute(
            "INSERT OR REPLACE INTO aaru_coverage_cells "
            "(snapshot_id, source, dataset, universe, period_start, period_end, "
            "expected_count, present_count, missing_count, status, reason, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                exchange,
                "cashEOD",
                "all",
                start or "",
                end or "",
                None,
                trading_dates,
                None,
                "partial",
                "Present trading dates are known; expected exchange-session reconciliation remains pending.",
                observed_at,
            ),
        )

    screener = inventory.get("screener", {})
    statuses = screener.get("statuses", {}) if isinstance(screener, Mapping) else {}
    for status, count in statuses.items():
        mapped = {
            "pages_collected": "complete",
            "partial": "partial",
            "pending": "retryable",
            "unresolved": "unknown",
            "routed_to_fund_dataset": "not_applicable",
        }.get(status, "unknown")
        connection.execute(
            "INSERT OR REPLACE INTO aaru_coverage_cells "
            "(snapshot_id, source, dataset, universe, period_start, period_end, "
            "expected_count, present_count, missing_count, status, reason, observed_at) "
            "VALUES (?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                "SCREENER",
                "fundamentals:{}".format(status),
                "expanded_universe",
                count,
                count if mapped == "complete" else 0,
                count if mapped in {"retryable", "partial", "unknown"} else 0,
                mapped,
                "Imported from Screener resume-v3 hard inventory.",
                observed_at,
            ),
        )


def import_missing_manifest(
    connection: sqlite3.Connection,
    manifest: Mapping[str, Any],
) -> None:
    for value in manifest.get("items", []):
        connection.execute(
            "INSERT OR REPLACE INTO aaru_missing_manifest "
            "(source, dataset, identity, period_start, period_end, status, "
            "attempts, next_action, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                value.get("source", ""),
                value.get("dataset", ""),
                value.get("identity") or "",
                value.get("period_start") or "",
                value.get("period_end") or "",
                value.get("status", "unknown"),
                int(value.get("attempts", 0) or 0),
                value.get("next_action"),
                value.get("reason"),
            ),
        )


def adapt_database(
    source: Path,
    destination: Path,
    schema_path: Path,
    *,
    inventory_path: Optional[Path] = None,
    missing_manifest_path: Optional[Path] = None,
    source_branch: str = "abhijitsingh14-run-stock-market-app",
    source_commit_sha: str = "",
    retain_source_snapshot: Optional[Path] = None,
) -> Dict[str, Any]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    consistent_backup(source, destination)
    pre_adaptation_hash = sha256_file(destination)
    if retain_source_snapshot:
        retained = retain_source_snapshot.expanduser().resolve()
        if retained.exists():
            destination.unlink(missing_ok=True)
            raise FileExistsError(retained)
        retained.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, retained)
        if sha256_file(retained) != pre_adaptation_hash:
            destination.unlink(missing_ok=True)
            retained.unlink(missing_ok=True)
            raise RuntimeError("Retained source snapshot hash mismatch")

    connection = sqlite3.connect(destination)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        ensure_integrity(connection)
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        snapshot_id = "stock-market-{}".format(pre_adaptation_hash[:24])
        connection.execute(
            "INSERT INTO aaru_source_snapshots "
            "(snapshot_id, source_path, snapshot_sha256, source_branch, "
            "source_commit_sha, created_at, source_size_bytes, "
            "snapshot_size_bytes, integrity_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                str(source),
                pre_adaptation_hash,
                source_branch,
                source_commit_sha,
                utcnow(),
                source.stat().st_size,
                destination.stat().st_size,
                "ok",
            ),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO aaru_dataset_registry "
            "(dataset, source_table, aaru_kind, authority, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            dataset_mappings(),
        )
        if inventory_path:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            import_inventory(connection, snapshot_id, inventory)
        if missing_manifest_path:
            manifest = json.loads(
                missing_manifest_path.read_text(encoding="utf-8")
            )
            import_missing_manifest(connection, manifest)
        connection.commit()
        ensure_integrity(connection)
    except BaseException:
        connection.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        connection.close()

    result = {
        "schema": "aaru.market-adaptation-receipt.v2",
        "created_at": utcnow(),
        "source": str(source),
        "destination": str(destination),
        "source_snapshot_sha256": pre_adaptation_hash,
        "candidate_sha256": sha256_file(destination),
        "source_bytes": source.stat().st_size,
        "candidate_bytes": destination.stat().st_size,
        "source_branch": source_branch,
        "source_commit_sha": source_commit_sha,
        "retained_source_snapshot": (
            str(retain_source_snapshot) if retain_source_snapshot else None
        ),
        "integrity": "ok",
    }
    receipt = destination.with_suffix(destination.suffix + ".aaru-receipt.json")
    receipt.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["receipt"] = str(receipt)
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--missing-manifest", type=Path)
    parser.add_argument(
        "--source-branch",
        default="abhijitsingh14-run-stock-market-app",
    )
    parser.add_argument("--source-commit-sha", default="")
    parser.add_argument("--retain-source-snapshot", type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    schema = args.schema or (
        Path(__file__).resolve().parents[1]
        / "schema"
        / "aaru_market_staging.sql"
    )
    result = adapt_database(
        args.source,
        args.destination,
        schema,
        inventory_path=args.inventory,
        missing_manifest_path=args.missing_manifest,
        source_branch=args.source_branch,
        source_commit_sha=args.source_commit_sha,
        retain_source_snapshot=args.retain_source_snapshot,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

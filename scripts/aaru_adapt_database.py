#!/usr/bin/env python3
"""Create a safe AARU metadata or full-copy candidate from stock_market.db."""

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
        "file:{}?mode=ro".format(source), uri=True, timeout=60
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


def preflight_full_copy(source: Path, destination: Path, reserve_gib: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination.parent).free
    reserve = int(reserve_gib * 1024 ** 3)
    required = int(source.stat().st_size * 1.15) + reserve
    if free < required:
        raise RuntimeError(
            "Insufficient free space for full copy: free={:.2f} GiB, "
            "required={:.2f} GiB. Use --mode metadata or free space.".format(
                free / 1024 ** 3, required / 1024 ** 3
            )
        )


def existing_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def create_conditional_views(connection: sqlite3.Connection) -> None:
    tables = existing_tables(connection)
    views = []
    if {"securities", "exchange_symbols"}.issubset(tables):
        views.append(
            """
            CREATE VIEW IF NOT EXISTS aaru_instruments AS
            SELECT s.id AS security_id,s.canonical_id,s.isin,s.name,s.active,
                   es.exchange,es.exchange_symbol,es.series,es.scrip_code,
                   es.active AS listing_active,es.source
            FROM securities s JOIN exchange_symbols es ON es.security_id=s.id
            """
        )
    if "daily_prices" in tables:
        views.append(
            """
            CREATE VIEW IF NOT EXISTS aaru_cash_eod AS
            SELECT security_id,exchange,trading_date AS event_time,series,
                   open,high,low,close,last,previous_close,volume,turnover,
                   trades,deliverable_quantity,source,
                   created_at AS retrieved_at,updated_at
            FROM daily_prices
            """
        )
    if "filings" in tables:
        views.append(
            """
            CREATE VIEW IF NOT EXISTS aaru_filing_index AS
            SELECT id AS filing_id,security_id,symbol,exchange,dataset,
                   external_id,filing_date AS knowledge_time,
                   period_start AS event_period_start,
                   period_end AS event_period_end,document_url,
                   document_sha256,is_revision,created_at AS retrieved_at
            FROM filings
            """
        )
    if "financial_fact_instances" in tables:
        views.append(
            """
            CREATE VIEW IF NOT EXISTS aaru_financial_facts AS
            SELECT * FROM financial_fact_instances
            """
        )
    if "financial_metrics" in tables:
        views.append(
            """
            CREATE VIEW IF NOT EXISTS aaru_financial_metrics AS
            SELECT filing_id,symbol,filing_date AS knowledge_time,
                   period_end AS event_time,metric,value,source,scope,derivation
            FROM financial_metrics
            """
        )
    if "shareholding_patterns" in tables:
        views.append(
            """
            CREATE VIEW IF NOT EXISTS aaru_ownership AS
            SELECT filing_id,security_id,symbol,quarter_end AS event_time,
                   promoter_percent,fii_percent,dii_percent,public_percent,
                   non_institution_public_percent
            FROM shareholding_patterns
            """
        )
    for statement in views:
        connection.execute(statement)


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
            "(snapshot_id,source,dataset,universe,period_start,period_end,"
            "expected_count,present_count,missing_count,status,reason,observed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
                "Present coverage known via {}; expected exchange sessions still "
                "require reconciliation.".format(
                    inventory.get("price_coverage_method", "unknown")
                ),
                observed_at,
            ),
        )
    screener = inventory.get("screener", {})
    statuses = screener.get("statuses", {}) if isinstance(screener, Mapping) else {}
    for source_status, count in statuses.items():
        mapped = {
            "pages_collected": "complete",
            "partial": "partial",
            "pending": "retryable",
            "unresolved": "unknown",
            "routed_to_fund_dataset": "not_applicable",
        }.get(source_status, "unknown")
        connection.execute(
            "INSERT OR REPLACE INTO aaru_coverage_cells "
            "(snapshot_id,source,dataset,universe,period_start,period_end,"
            "expected_count,present_count,missing_count,status,reason,observed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id,
                "SCREENER",
                "fundamentals:{}".format(source_status),
                "expanded_universe",
                "",
                "",
                count,
                count if mapped == "complete" else 0,
                count if mapped in {"retryable", "partial", "unknown"} else 0,
                mapped,
                "Imported from Screener hard inventory.",
                observed_at,
            ),
        )


def import_manifest(
    connection: sqlite3.Connection, manifest: Mapping[str, Any]
) -> None:
    for value in manifest.get("items", []):
        connection.execute(
            "INSERT OR REPLACE INTO aaru_missing_manifest "
            "(source,dataset,identity,period_start,period_end,status,attempts,"
            "next_action,reason,locator,affected_count) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
                value.get("locator"),
                int(value.get("affected_count", 1) or 1),
            ),
        )


def adapt_database(
    source: Path,
    destination: Path,
    schema_path: Path,
    *,
    mode: str = "metadata",
    inventory_path: Optional[Path] = None,
    missing_manifest_path: Optional[Path] = None,
    source_branch: str = "abhijitsingh14-run-stock-market-app",
    source_commit_sha: str = "",
    reserve_gib: float = 12.0,
) -> Dict[str, Any]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if mode == "full":
        preflight_full_copy(source, destination, reserve_gib)
        consistent_backup(source, destination)
        snapshot_hash = sha256_file(destination)
        snapshot_size = destination.stat().st_size
    elif mode == "metadata":
        sqlite3.connect(destination).close()
        inventory_hash = None
        if inventory_path:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            inventory_hash = inventory.get("database", {}).get("sha256")
        snapshot_hash = inventory_hash or "unsealed-live-source"
        snapshot_size = 0
    else:
        raise ValueError("Unsupported mode: {}".format(mode))

    connection = sqlite3.connect(destination)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        if mode == "full":
            ensure_integrity(connection)
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        snapshot_id = "stock-market-{}".format(
            snapshot_hash[:24] if snapshot_hash != "unsealed-live-source"
            else "unsealed"
        )
        connection.execute(
            "INSERT INTO aaru_source_snapshots "
            "(snapshot_id,source_path,snapshot_sha256,source_branch,"
            "source_commit_sha,created_at,source_size_bytes,"
            "snapshot_size_bytes,integrity_status,copy_mode) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id,
                str(source),
                snapshot_hash,
                source_branch,
                source_commit_sha,
                utcnow(),
                source.stat().st_size,
                snapshot_size,
                "ok" if mode == "full" else "not_run",
                mode,
            ),
        )
        if inventory_path:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            import_inventory(connection, snapshot_id, inventory)
        if missing_manifest_path:
            import_manifest(
                connection,
                json.loads(missing_manifest_path.read_text(encoding="utf-8")),
            )
        if mode == "full":
            create_conditional_views(connection)
        connection.commit()
        if mode == "full":
            ensure_integrity(connection)
    except BaseException:
        connection.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        connection.close()

    result = {
        "schema": "aaru.market-adaptation-receipt.v3",
        "created_at": utcnow(),
        "mode": mode,
        "source": str(source),
        "destination": str(destination),
        "source_snapshot_sha256": snapshot_hash,
        "candidate_sha256": sha256_file(destination),
        "source_bytes": source.stat().st_size,
        "candidate_bytes": destination.stat().st_size,
        "source_branch": source_branch,
        "source_commit_sha": source_commit_sha,
        "integrity": "ok" if mode == "full" else "not_run",
    }
    receipt = destination.with_suffix(destination.suffix + ".aaru-receipt.json")
    receipt.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["receipt"] = str(receipt)
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--mode", choices=("metadata", "full"), default="metadata")
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--missing-manifest", type=Path)
    parser.add_argument(
        "--source-branch", default="abhijitsingh14-run-stock-market-app"
    )
    parser.add_argument("--source-commit-sha", default="")
    parser.add_argument("--reserve-gib", type=float, default=12.0)
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
        mode=args.mode,
        inventory_path=args.inventory,
        missing_manifest_path=args.missing_manifest,
        source_branch=args.source_branch,
        source_commit_sha=args.source_commit_sha,
        reserve_gib=args.reserve_gib,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build a read-only hard inventory for the local NSE/BSE/Screener data estate."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

KNOWN_TABLES = (
    "securities",
    "exchange_symbols",
    "daily_prices",
    "ingestion_runs",
    "ingestion_checkpoints",
    "ingestion_errors",
    "archive_availability",
    "filings",
    "raw_documents",
    "shareholding_patterns",
    "financial_facts",
    "financial_fact_instances",
    "financial_metrics",
    "corporate_actions",
    "board_meetings",
    "pit_disclosures",
    "sast_disclosures",
    "institutional_activity",
    "market_metrics",
    "disclosure_errors",
    "filing_index_checkpoints",
    "filing_document_status",
    "bse_financial_checkpoints",
)


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


def fetch_rows(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] = (),
) -> List[List[Any]]:
    try:
        return [list(row) for row in connection.execute(sql, parameters).fetchall()]
    except sqlite3.Error as exc:
        return [["query_error", type(exc).__name__, str(exc)]]


def fetch_scalar(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] = (),
) -> Optional[Any]:
    try:
        row = connection.execute(sql, parameters).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def existing_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def exact_table_counts(
    connection: sqlite3.Connection,
    table_names: Iterable[str],
) -> Dict[str, Optional[int]]:
    counts: Dict[str, Optional[int]] = {}
    for table_name in table_names:
        escaped = table_name.replace('"', '""')
        counts[table_name] = fetch_scalar(
            connection, 'SELECT COUNT(*) FROM "{}"'.format(escaped)
        )
    return counts


def database_storage(connection: sqlite3.Connection, database: Path) -> Dict[str, Any]:
    page_size = int(fetch_scalar(connection, "PRAGMA page_size") or 0)
    page_count = int(fetch_scalar(connection, "PRAGMA page_count") or 0)
    freelist_count = int(fetch_scalar(connection, "PRAGMA freelist_count") or 0)
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    return {
        "main_bytes": database.stat().st_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "shm_bytes": shm.stat().st_size if shm.exists() else 0,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "logical_bytes": page_size * page_count,
        "reusable_bytes": page_size * freelist_count,
    }


def inventory_database(
    database: Path,
    *,
    include_hash: bool = False,
    integrity_mode: str = "quick",
) -> Dict[str, Any]:
    database = database.expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(database)

    connection = sqlite3.connect(
        "file:{}?mode=ro".format(database),
        uri=True,
        timeout=30,
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("BEGIN")
    try:
        tables = existing_tables(connection)
        counts = exact_table_counts(
            connection, [name for name in KNOWN_TABLES if name in tables]
        )
        integrity: List[str]
        if integrity_mode == "full":
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        elif integrity_mode == "quick":
            integrity = [row[0] for row in connection.execute("PRAGMA quick_check")]
        else:
            integrity = ["not_run"]

        report: Dict[str, Any] = {
            "schema": "aaru.market-hard-inventory.v2",
            "observed_at": utcnow(),
            "database": {
                "path": str(database),
                "storage": database_storage(connection, database),
                "sha256": sha256_file(database) if include_hash else None,
                "hash_note": (
                    "Hash taken from the current main database file. For a sealed "
                    "identity, hash a consistent SQLite backup after writers stop."
                    if include_hash
                    else "Not requested; hash a consistent SQLite backup for sealing."
                ),
            },
            "integrity_check": integrity,
            "foreign_key_violations": fetch_rows(
                connection, "PRAGMA foreign_key_check"
            ),
            "table_counts": counts,
            "price_coverage": fetch_rows(
                connection,
                "SELECT exchange, COUNT(*), COUNT(DISTINCT trading_date), "
                "MIN(trading_date), MAX(trading_date), COUNT(DISTINCT security_id) "
                "FROM daily_prices GROUP BY exchange ORDER BY exchange",
            )
            if "daily_prices" in tables
            else [],
            "price_checkpoint_status": fetch_rows(
                connection,
                "SELECT source, status, COUNT(*), MIN(checkpoint_date), "
                "MAX(checkpoint_date) FROM ingestion_checkpoints "
                "WHERE dataset='daily_prices' GROUP BY source, status "
                "ORDER BY source, status",
            )
            if "ingestion_checkpoints" in tables
            else [],
            "archive_availability": fetch_rows(
                connection,
                "SELECT source, status, COUNT(*), MIN(year_month), MAX(year_month), "
                "SUM(success_count), SUM(not_published_count) "
                "FROM archive_availability GROUP BY source, status "
                "ORDER BY source, status",
            )
            if "archive_availability" in tables
            else [],
            "filing_coverage": fetch_rows(
                connection,
                "SELECT exchange, dataset, COUNT(*), COUNT(DISTINCT symbol), "
                "MIN(filing_date), MAX(filing_date), MIN(period_end), MAX(period_end) "
                "FROM filings GROUP BY exchange, dataset "
                "ORDER BY exchange, dataset",
            )
            if "filings" in tables
            else [],
            "document_status": fetch_rows(
                connection,
                "SELECT exchange, dataset, status, COUNT(*), SUM(attempts) "
                "FROM filing_document_status GROUP BY exchange, dataset, status "
                "ORDER BY exchange, dataset, status",
            )
            if "filing_document_status" in tables
            else [],
            "filing_index_checkpoints": fetch_rows(
                connection,
                "SELECT exchange, dataset, status, COUNT(*), SUM(row_count), "
                "SUM(processed_count), SUM(failed_count), MIN(window_start), "
                "MAX(window_end) FROM filing_index_checkpoints "
                "GROUP BY exchange, dataset, status "
                "ORDER BY exchange, dataset, status",
            )
            if "filing_index_checkpoints" in tables
            else [],
            "bse_financial_checkpoints": fetch_rows(
                connection,
                "SELECT status, COUNT(*), SUM(row_count), SUM(document_count), "
                "SUM(failed_count), MIN(range_start), MAX(range_end) "
                "FROM bse_financial_checkpoints GROUP BY status ORDER BY status",
            )
            if "bse_financial_checkpoints" in tables
            else [],
            "financial_metric_coverage": fetch_rows(
                connection,
                "SELECT scope, source, metric, COUNT(*), COUNT(DISTINCT symbol), "
                "MIN(period_end), MAX(period_end) FROM financial_metrics "
                "GROUP BY scope, source, metric ORDER BY scope, source, metric",
            )
            if "financial_metrics" in tables
            else [],
            "shareholding_coverage": fetch_rows(
                connection,
                "SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(quarter_end), "
                "MAX(quarter_end), SUM(promoter_percent IS NULL), "
                "SUM(fii_percent IS NULL), SUM(dii_percent IS NULL), "
                "SUM(public_percent IS NULL) FROM shareholding_patterns",
            )
            if "shareholding_patterns" in tables
            else [],
            "instrument_identity": fetch_rows(
                connection,
                "SELECT COUNT(*), SUM(isin IS NULL OR isin=''), "
                "SUM(active=1), SUM(active=0) FROM securities",
            )
            if "securities" in tables
            else [],
            "exchange_symbol_coverage": fetch_rows(
                connection,
                "SELECT exchange, COUNT(*), COUNT(DISTINCT security_id), "
                "SUM(active=1), SUM(active=0), SUM(isin_missing) FROM ("
                "SELECT es.*, CASE WHEN s.isin IS NULL OR s.isin='' THEN 1 ELSE 0 END "
                "AS isin_missing FROM exchange_symbols es JOIN securities s "
                "ON s.id=es.security_id) GROUP BY exchange ORDER BY exchange",
            )
            if {"securities", "exchange_symbols"}.issubset(tables)
            else [],
        }
        return report
    finally:
        connection.rollback()
        connection.close()


def inventory_screener(root: Path) -> Dict[str, Any]:
    root = root.expanduser().resolve()
    manifest = root / "MANIFESTS/SCREENER_RESUME_V3/summary.json"
    coverage = root / "MANIFESTS/SCREENER_RESUME_V3/coverage.csv"
    result: Dict[str, Any] = {
        "root": str(root),
        "summary": None,
        "coverage_rows": 0,
        "statuses": {},
    }
    if manifest.exists():
        result["summary"] = json.loads(manifest.read_text(encoding="utf-8"))
    if coverage.exists():
        status_counts: Counter[str] = Counter()
        with coverage.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                result["coverage_rows"] += 1
                status_counts[row.get("status") or "unknown"] += 1
        result["statuses"] = dict(sorted(status_counts.items()))
    return result


def render_markdown(report: Mapping[str, Any]) -> str:
    database = report["database"]
    storage = database["storage"]
    lines = [
        "# AARU Market Data Hard Inventory",
        "",
        "Observed: `{}`".format(report["observed_at"]),
        "",
        "## Database",
        "",
        "- Path: `{}`".format(database["path"]),
        "- Main file: `{:.2f} GiB`".format(storage["main_bytes"] / 1024 ** 3),
        "- WAL: `{:.2f} GiB`".format(storage["wal_bytes"] / 1024 ** 3),
        "- Reusable pages: `{:.2f} GiB`".format(
            storage["reusable_bytes"] / 1024 ** 3
        ),
        "- Integrity: `{}`".format(", ".join(report["integrity_check"])),
        "",
        "## Price coverage",
        "",
        "| Exchange | Rows | Trading dates | First | Last | Securities |",
        "|---|---:|---:|---|---|---:|",
    ]
    for row in report.get("price_coverage", []):
        if row and row[0] == "query_error":
            continue
        lines.append("| {} | {} | {} | {} | {} | {} |".format(*row))
    screener = report.get("screener")
    if screener:
        lines += ["", "## Screener", ""]
        for status, count in screener.get("statuses", {}).items():
            lines.append("- `{}`: {}".format(status, count))
    lines += ["", "## Table counts", ""]
    for table, count in sorted(report.get("table_counts", {}).items()):
        lines.append("- `{}`: {}".format(table, count))
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--screener-root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--hash-database", action="store_true")
    parser.add_argument(
        "--integrity",
        choices=("none", "quick", "full"),
        default="quick",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    report = inventory_database(
        args.database,
        include_hash=args.hash_database,
        integrity_mode=args.integrity,
    )
    if args.screener_root:
        report["screener"] = inventory_screener(args.screener_root)
    json_path = args.output / "inventory.json"
    markdown_path = args.output / "inventory.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json_path)
    print(markdown_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

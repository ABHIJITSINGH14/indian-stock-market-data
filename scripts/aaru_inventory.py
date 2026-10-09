#!/usr/bin/env python3
"""Build a bounded, read-only inventory for the NSE/BSE/Screener data estate.

Profiles:
- live: safe while WAL writers are active; avoids long scans, integrity checks,
  hashes, and a pinned read transaction.
- snapshot: exact counts and integrity checks for a consistent stopped/backup DB.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

LARGE_TABLES = {
    "daily_prices",
    "raw_documents",
    "financial_facts",
    "financial_fact_instances",
    "financial_metrics",
}
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
    "bulk_deals",
    "block_deals",
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


@contextmanager
def query_deadline(connection: sqlite3.Connection, seconds: float) -> Iterator[None]:
    if seconds <= 0:
        yield
        return
    deadline = time.monotonic() + seconds

    def progress() -> int:
        return 1 if time.monotonic() > deadline else 0

    connection.set_progress_handler(progress, 10_000)
    try:
        yield
    finally:
        connection.set_progress_handler(None, 0)


def fetch_rows(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] = (),
    *,
    budget_seconds: float = 0,
) -> List[List[Any]]:
    try:
        with query_deadline(connection, budget_seconds):
            return [list(row) for row in connection.execute(sql, parameters).fetchall()]
    except sqlite3.Error as exc:
        return [["query_error", type(exc).__name__, str(exc)]]


def fetch_scalar(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] = (),
    *,
    budget_seconds: float = 0,
) -> Optional[Any]:
    try:
        with query_deadline(connection, budget_seconds):
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


def table_count_inventory(
    connection: sqlite3.Connection,
    table_names: Iterable[str],
    *,
    profile: str,
    query_budget_seconds: float,
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for table_name in table_names:
        escaped = table_name.replace('"', '""')
        if profile == "snapshot" or table_name not in LARGE_TABLES:
            value = fetch_scalar(
                connection,
                'SELECT COUNT(*) FROM "{}"'.format(escaped),
                budget_seconds=query_budget_seconds,
            )
            result[table_name] = {
                "value": value,
                "method": "exact" if value is not None else "timed_out",
            }
            continue
        value = fetch_scalar(
            connection,
            'SELECT MAX(rowid) FROM "{}"'.format(escaped),
            budget_seconds=min(2.0, query_budget_seconds),
        )
        result[table_name] = {
            "value": value,
            "method": "max_rowid_upper_bound" if value is not None else "not_counted",
        }
    return result


def database_storage(connection: sqlite3.Connection, database: Path) -> Dict[str, Any]:
    page_size = int(fetch_scalar(connection, "PRAGMA page_size") or 0)
    page_count = int(fetch_scalar(connection, "PRAGMA page_count") or 0)
    freelist_count = int(fetch_scalar(connection, "PRAGMA freelist_count") or 0)
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    stat = shutil.disk_usage(database.parent)
    return {
        "main_bytes": database.stat().st_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "shm_bytes": shm.stat().st_size if shm.exists() else 0,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "logical_bytes": page_size * page_count,
        "reusable_bytes": page_size * freelist_count,
        "filesystem_total_bytes": stat.total,
        "filesystem_used_bytes": stat.used,
        "filesystem_free_bytes": stat.free,
    }


def export_query_csv(
    connection: sqlite3.Connection,
    destination: Path,
    sql: str,
    headers: Sequence[str],
    *,
    limit: int,
) -> Dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    truncated = False
    error: Optional[str] = None
    try:
        cursor = connection.execute(sql)
        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            for row in cursor:
                if count >= limit:
                    truncated = True
                    break
                writer.writerow(list(row))
                count += 1
    except sqlite3.Error as exc:
        error = "{}: {}".format(type(exc).__name__, exc)
    return {
        "path": str(destination),
        "rows": count,
        "truncated": truncated,
        "error": error,
    }


def inventory_screener(
    root: Path,
    *,
    detail_output: Optional[Path] = None,
    detail_limit: int = 250_000,
) -> Dict[str, Any]:
    root = root.expanduser().resolve()
    manifest = root / "MANIFESTS/SCREENER_RESUME_V3/summary.json"
    coverage = root / "MANIFESTS/SCREENER_RESUME_V3/coverage.csv"
    result: Dict[str, Any] = {
        "root": str(root),
        "summary": None,
        "coverage_rows": 0,
        "statuses": {},
        "detail": None,
    }
    if manifest.exists():
        result["summary"] = json.loads(manifest.read_text(encoding="utf-8"))
    if not coverage.exists():
        return result

    status_counts: Counter[str] = Counter()
    detail_handle = None
    detail_writer = None
    copied = 0
    truncated = False
    try:
        if detail_output:
            detail_output.parent.mkdir(parents=True, exist_ok=True)
            detail_handle = detail_output.open("w", newline="", encoding="utf-8")
        with coverage.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if detail_handle is not None:
                detail_writer = csv.DictWriter(
                    detail_handle, fieldnames=reader.fieldnames or []
                )
                detail_writer.writeheader()
            for row in reader:
                result["coverage_rows"] += 1
                status_counts[row.get("status") or "unknown"] += 1
                if detail_writer is not None:
                    if copied >= detail_limit:
                        truncated = True
                    else:
                        detail_writer.writerow(row)
                        copied += 1
    finally:
        if detail_handle is not None:
            detail_handle.close()
    result["statuses"] = dict(sorted(status_counts.items()))
    if detail_output:
        result["detail"] = {
            "path": str(detail_output),
            "rows": copied,
            "truncated": truncated,
        }
    return result


def inventory_database(
    database: Path,
    *,
    profile: str = "live",
    include_hash: bool = False,
    integrity_mode: str = "none",
    query_budget_seconds: float = 5.0,
    detail_directory: Optional[Path] = None,
    detail_limit: int = 250_000,
) -> Dict[str, Any]:
    database = database.expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    if profile == "live" and include_hash:
        raise ValueError(
            "Refusing to hash a live WAL database; create/hash a consistent backup."
        )
    if profile == "live" and integrity_mode != "none":
        raise ValueError(
            "Integrity scans are disabled in live profile; use a consistent snapshot."
        )

    connection = sqlite3.connect(
        "file:{}?mode=ro".format(database),
        uri=True,
        timeout=5 if profile == "live" else 30,
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute(
        "PRAGMA busy_timeout={}".format(2_000 if profile == "live" else 30_000)
    )
    tables = existing_tables(connection)
    details: Dict[str, Any] = {}
    if detail_directory:
        detail_directory.mkdir(parents=True, exist_ok=True)

    if profile == "snapshot":
        connection.execute("BEGIN")
    try:
        counts = table_count_inventory(
            connection,
            [name for name in KNOWN_TABLES if name in tables],
            profile=profile,
            query_budget_seconds=query_budget_seconds,
        )
        if integrity_mode == "full":
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        elif integrity_mode == "quick":
            integrity = [row[0] for row in connection.execute("PRAGMA quick_check")]
        else:
            integrity = ["not_run"]
        foreign_keys = (
            fetch_rows(connection, "PRAGMA foreign_key_check")
            if profile == "snapshot"
            else [["not_run_in_live_profile"]]
        )

        if profile == "snapshot" and "daily_prices" in tables:
            price_coverage = fetch_rows(
                connection,
                "SELECT exchange, COUNT(*), COUNT(DISTINCT trading_date), "
                "MIN(trading_date), MAX(trading_date), COUNT(DISTINCT security_id) "
                "FROM daily_prices GROUP BY exchange ORDER BY exchange",
                budget_seconds=query_budget_seconds,
            )
            price_coverage_method = "daily_prices_exact"
        elif "ingestion_checkpoints" in tables:
            price_coverage = fetch_rows(
                connection,
                "SELECT UPPER(source), SUM(row_count), COUNT(*), "
                "MIN(checkpoint_date), MAX(checkpoint_date), NULL "
                "FROM ingestion_checkpoints "
                "WHERE dataset='daily_prices' AND status='success' "
                "GROUP BY source ORDER BY source",
                budget_seconds=query_budget_seconds,
            )
            price_coverage_method = "successful_checkpoints"
        else:
            price_coverage = []
            price_coverage_method = "unavailable"

        report: Dict[str, Any] = {
            "schema": "aaru.market-hard-inventory.v3",
            "observed_at": utcnow(),
            "profile": profile,
            "consistency": (
                "consistent_read_transaction"
                if profile == "snapshot"
                else "bounded_live_observation"
            ),
            "database": {
                "path": str(database),
                "storage": database_storage(connection, database),
                "sha256": sha256_file(database) if include_hash else None,
                "hash_note": (
                    "Hash of a consistent snapshot."
                    if include_hash
                    else "Not sealed; hash a consistent backup for promotion."
                ),
            },
            "integrity_check": integrity,
            "foreign_key_violations": foreign_keys,
            "table_counts": counts,
            "price_coverage": price_coverage,
            "price_coverage_method": price_coverage_method,
            "price_checkpoint_status": fetch_rows(
                connection,
                "SELECT source, status, COUNT(*), MIN(checkpoint_date), "
                "MAX(checkpoint_date), SUM(row_count) "
                "FROM ingestion_checkpoints WHERE dataset='daily_prices' "
                "GROUP BY source, status ORDER BY source, status",
                budget_seconds=query_budget_seconds,
            )
            if "ingestion_checkpoints" in tables
            else [],
            "archive_availability": fetch_rows(
                connection,
                "SELECT source, status, COUNT(*), MIN(year_month), MAX(year_month), "
                "SUM(success_count), SUM(not_published_count) "
                "FROM archive_availability GROUP BY source, status "
                "ORDER BY source, status",
                budget_seconds=query_budget_seconds,
            )
            if "archive_availability" in tables
            else [],
            "document_status": fetch_rows(
                connection,
                "SELECT exchange, dataset, status, COUNT(*), SUM(attempts) "
                "FROM filing_document_status GROUP BY exchange, dataset, status "
                "ORDER BY exchange, dataset, status",
                budget_seconds=query_budget_seconds,
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
                budget_seconds=query_budget_seconds,
            )
            if "filing_index_checkpoints" in tables
            else [],
            "bse_financial_checkpoints": fetch_rows(
                connection,
                "SELECT status, COUNT(*), SUM(row_count), SUM(document_count), "
                "SUM(failed_count), MIN(range_start), MAX(range_end) "
                "FROM bse_financial_checkpoints GROUP BY status ORDER BY status",
                budget_seconds=query_budget_seconds,
            )
            if "bse_financial_checkpoints" in tables
            else [],
            "instrument_identity": fetch_rows(
                connection,
                "SELECT COUNT(*), SUM(isin IS NULL OR isin=''), "
                "SUM(active=1), SUM(active=0) FROM securities",
                budget_seconds=query_budget_seconds,
            )
            if "securities" in tables
            else [],
            "details": details,
        }

        if detail_directory and "ingestion_checkpoints" in tables:
            details["price_checkpoints"] = export_query_csv(
                connection,
                detail_directory / "price_checkpoints.csv",
                "SELECT source,dataset,checkpoint_key,checkpoint_date,status,"
                "row_count,error,updated_at FROM ingestion_checkpoints "
                "WHERE dataset='daily_prices' AND status NOT IN "
                "('success','holiday','not_published') "
                "ORDER BY source,checkpoint_date",
                (
                    "source", "dataset", "checkpoint_key", "checkpoint_date",
                    "status", "row_count", "error", "updated_at",
                ),
                limit=detail_limit,
            )
        if detail_directory and "filing_index_checkpoints" in tables:
            details["filing_index_checkpoints"] = export_query_csv(
                connection,
                detail_directory / "filing_index_checkpoints.csv",
                "SELECT exchange,dataset,window_start,window_end,status,attempts,"
                "row_count,processed_count,failed_count,error "
                "FROM filing_index_checkpoints WHERE status!='complete' "
                "ORDER BY exchange,dataset,window_start",
                (
                    "exchange", "dataset", "window_start", "window_end",
                    "status", "attempts", "row_count", "processed_count",
                    "failed_count", "error",
                ),
                limit=detail_limit,
            )
        if detail_directory and "filing_document_status" in tables:
            details["filing_document_status"] = export_query_csv(
                connection,
                detail_directory / "filing_document_status.csv",
                "SELECT exchange,dataset,external_id,document_url,status,attempts,"
                "document_sha256,error_type,error,updated_at "
                "FROM filing_document_status WHERE status!='complete' "
                "ORDER BY exchange,dataset,external_id",
                (
                    "exchange", "dataset", "external_id", "document_url",
                    "status", "attempts", "document_sha256", "error_type",
                    "error", "updated_at",
                ),
                limit=detail_limit,
            )
        if detail_directory and "bse_financial_checkpoints" in tables:
            details["bse_financial_checkpoints"] = export_query_csv(
                connection,
                detail_directory / "bse_financial_checkpoints.csv",
                "SELECT scrip_code,range_start,range_end,status,attempts,row_count,"
                "document_count,failed_count,error,updated_at "
                "FROM bse_financial_checkpoints WHERE status!='complete' "
                "ORDER BY scrip_code",
                (
                    "scrip_code", "range_start", "range_end", "status",
                    "attempts", "row_count", "document_count", "failed_count",
                    "error", "updated_at",
                ),
                limit=detail_limit,
            )
        return report
    finally:
        if profile == "snapshot":
            connection.rollback()
        connection.close()


def render_markdown(report: Mapping[str, Any]) -> str:
    database = report["database"]
    storage = database["storage"]
    lines = [
        "# AARU Market Data Hard Inventory",
        "",
        "Observed: `{}`".format(report["observed_at"]),
        "",
        "- Profile: `{}`".format(report["profile"]),
        "- Consistency: `{}`".format(report["consistency"]),
        "- Database: `{}`".format(database["path"]),
        "- Main file: `{:.2f} GiB`".format(storage["main_bytes"] / 1024 ** 3),
        "- WAL: `{:.2f} GiB`".format(storage["wal_bytes"] / 1024 ** 3),
        "- Free filesystem space: `{:.2f} GiB`".format(
            storage["filesystem_free_bytes"] / 1024 ** 3
        ),
        "- Reusable DB pages: `{:.2f} GiB`".format(
            storage["reusable_bytes"] / 1024 ** 3
        ),
        "- Integrity: `{}`".format(", ".join(report["integrity_check"])),
        "",
        "## Price coverage",
        "",
        "Method: `{}`".format(report["price_coverage_method"]),
        "",
        "| Exchange | Rows/checkpoint rows | Trading dates | First | Last | Securities |",
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
    lines += ["", "## Table count observations", ""]
    for table, observation in sorted(report.get("table_counts", {}).items()):
        lines.append(
            "- `{}`: {} ({})".format(
                table, observation.get("value"), observation.get("method")
            )
        )
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--screener-root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", choices=("live", "snapshot"), default="live")
    parser.add_argument("--hash-database", action="store_true")
    parser.add_argument(
        "--integrity", choices=("none", "quick", "full"), default="none"
    )
    parser.add_argument("--query-budget-seconds", type=float, default=5.0)
    parser.add_argument("--detail-limit", type=int, default=250_000)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    detail_directory = args.output / "details"
    report = inventory_database(
        args.database,
        profile=args.profile,
        include_hash=args.hash_database,
        integrity_mode=args.integrity,
        query_budget_seconds=args.query_budget_seconds,
        detail_directory=detail_directory,
        detail_limit=args.detail_limit,
    )
    if args.screener_root:
        report["screener"] = inventory_screener(
            args.screener_root,
            detail_output=detail_directory / "screener_coverage.csv",
            detail_limit=args.detail_limit,
        )
    json_path = args.output / "inventory.json"
    markdown_path = args.output / "inventory.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json_path)
    print(markdown_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

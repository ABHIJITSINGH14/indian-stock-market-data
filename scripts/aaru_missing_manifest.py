#!/usr/bin/env python3
"""Convert a hard inventory into a finite, auditable missing-data manifest."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

TERMINAL_STATUSES = {
    "complete",
    "source_unavailable",
    "not_published",
    "not_applicable",
}
RETRYABLE_SOURCE_STATUSES = {"failed", "pending", "interrupted", "retry"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def next_action(status: str) -> str:
    if status == "retryable":
        return "download"
    if status in {"partial", "blocked", "unknown"}:
        return "review"
    return "none"


def item(
    source: str,
    dataset: str,
    status: str,
    reason: str,
    *,
    identity: str = "",
    period_start: str = "",
    period_end: str = "",
    count: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "source": source,
        "dataset": dataset,
        "identity": identity,
        "period_start": period_start,
        "period_end": period_end,
        "status": status,
        "count": count,
        "reason": reason,
        "next_action": next_action(status),
    }


def build_missing_items(inventory: Mapping[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    for row in inventory.get("price_checkpoint_status", []):
        if not row or row[0] == "query_error":
            continue
        source, status, count, first_date, last_date = row
        if status == "failed":
            items.append(
                item(
                    source.upper(),
                    "cash_eod",
                    "retryable",
                    "Daily price checkpoints failed and remain eligible for retry.",
                    period_start=first_date or "",
                    period_end=last_date or "",
                    count=count,
                )
            )
        elif status == "not_published":
            items.append(
                item(
                    source.upper(),
                    "cash_eod",
                    "not_published",
                    "The official archive returned a terminal not-published state.",
                    period_start=first_date or "",
                    period_end=last_date or "",
                    count=count,
                )
            )

    for row in inventory.get("document_status", []):
        if not row or row[0] == "query_error":
            continue
        exchange, dataset, source_status, count, attempts = row
        if source_status == "complete":
            continue
        if source_status in RETRYABLE_SOURCE_STATUSES:
            status = "retryable"
        elif source_status == "unavailable":
            status = "source_unavailable"
        else:
            status = "unknown"
        items.append(
            item(
                exchange,
                dataset,
                status,
                "{} linked filing documents have source status {}; total attempts {}."
                .format(count, source_status, attempts or 0),
                count=count,
            )
        )

    for row in inventory.get("filing_index_checkpoints", []):
        if not row or row[0] == "query_error":
            continue
        (
            exchange,
            dataset,
            source_status,
            windows,
            row_count,
            processed_count,
            failed_count,
            start,
            end,
        ) = row
        if source_status == "complete":
            continue
        status = (
            "retryable"
            if source_status in RETRYABLE_SOURCE_STATUSES | {"partial"}
            else "unknown"
        )
        items.append(
            item(
                exchange,
                dataset,
                status,
                "{} index windows are {}; rows={}, processed={}, failed={}."
                .format(
                    windows,
                    source_status,
                    row_count or 0,
                    processed_count or 0,
                    failed_count or 0,
                ),
                period_start=start or "",
                period_end=end or "",
                count=windows,
            )
        )

    for row in inventory.get("bse_financial_checkpoints", []):
        if not row or row[0] == "query_error":
            continue
        source_status, scrips, rows, documents, failures, start, end = row
        if source_status == "complete":
            continue
        status = (
            "retryable"
            if source_status in RETRYABLE_SOURCE_STATUSES | {"partial"}
            else "unknown"
        )
        items.append(
            item(
                "BSE",
                "financial_results",
                status,
                "{} BSE scrip checkpoints are {}; rows={}, documents={}, failures={}."
                .format(scrips, source_status, rows or 0, documents or 0, failures or 0),
                period_start=start or "",
                period_end=end or "",
                count=scrips,
            )
        )

    screener = inventory.get("screener", {})
    statuses = screener.get("statuses", {}) if isinstance(screener, Mapping) else {}
    mapping = {
        "pending": (
            "retryable",
            "Public Screener pages have not yet been attempted or terminally classified.",
        ),
        "partial": (
            "partial",
            "At least one statement form/section remains incomplete.",
        ),
        "unresolved": (
            "unknown",
            "Instrument identity or page content needs manual/issuer-level resolution.",
        ),
        "routed_to_fund_dataset": (
            "not_applicable",
            "The instrument is not an ordinary operating-company fundamentals candidate.",
        ),
    }
    for source_status, (status, reason) in mapping.items():
        count = int(statuses.get(source_status, 0) or 0)
        if count:
            items.append(
                item("SCREENER", "fundamentals", status, reason, count=count)
            )

    required_tables = {
        "instrument_master": {"securities", "exchange_symbols"},
        "cash_eod": {"daily_prices"},
        "exchange_filings": {"filings", "raw_documents"},
        "financial_results": {"financial_fact_instances", "financial_metrics"},
        "shareholding": {"shareholding_patterns"},
        "corporate_actions": {"corporate_actions"},
        "insider_disclosures": {"pit_disclosures"},
        "sast_disclosures": {"sast_disclosures"},
    }
    present_tables = set(inventory.get("table_counts", {}).keys())
    for dataset, expected_tables in required_tables.items():
        absent = sorted(expected_tables - present_tables)
        if absent:
            items.append(
                item(
                    "LOCAL_DB",
                    dataset,
                    "unknown",
                    "Expected source tables are absent: {}.".format(", ".join(absent)),
                )
            )

    items.sort(
        key=lambda value: (
            value["next_action"] != "download",
            value["source"],
            value["dataset"],
            value["period_start"],
            value["identity"],
        )
    )
    return items


def summarize(items: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for value in items:
        status = str(value["status"])
        result[status] = result.get(status, 0) + 1
    return dict(sorted(result.items()))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    items = build_missing_items(inventory)
    payload = {
        "schema": "aaru.missing-manifest.v2",
        "generated_at": utcnow(),
        "inventory_schema": inventory.get("schema"),
        "summary": summarize(items),
        "items": items,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

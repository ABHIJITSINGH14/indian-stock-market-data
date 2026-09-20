#!/usr/bin/env python3
"""Convert AARU hard-inventory details into a finite missing-data manifest."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

TERMINAL = {"complete", "source_unavailable", "not_published", "not_applicable"}
STATUS_MAP = {
    "failed": "retryable",
    "pending": "retryable",
    "interrupted": "retryable",
    "retry": "retryable",
    "partial": "partial",
    "unavailable": "source_unavailable",
    "not_published": "not_published",
    "not_applicable": "not_applicable",
    "blocked": "blocked",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def next_action(status: str) -> str:
    if status == "retryable":
        return "download"
    if status in {"partial", "blocked", "unknown"}:
        return "review"
    return "none"


def make_item(
    source: str,
    dataset: str,
    status: str,
    reason: str,
    *,
    identity: str = "",
    period_start: str = "",
    period_end: str = "",
    attempts: int = 0,
    count: int = 1,
    locator: str = "",
) -> Dict[str, Any]:
    return {
        "source": source,
        "dataset": dataset,
        "identity": identity,
        "period_start": period_start,
        "period_end": period_end,
        "status": status,
        "attempts": attempts,
        "affected_count": count,
        "reason": reason,
        "locator": locator,
        "next_action": next_action(status),
    }


def rows_from_csv(path: Optional[str]) -> Iterator[Dict[str, str]]:
    if not path:
        return iter(())
    source = Path(path)
    if not source.exists():
        return iter(())

    def iterator() -> Iterator[Dict[str, str]]:
        with source.open(newline="", encoding="utf-8") as handle:
            yield from csv.DictReader(handle)

    return iterator()


def normalize_status(value: str) -> str:
    return STATUS_MAP.get((value or "").lower(), "unknown")


def detail_path(inventory: Mapping[str, Any], name: str) -> Optional[str]:
    value = inventory.get("details", {}).get(name)
    return str(value.get("path")) if isinstance(value, Mapping) else None


def dataset_contract_items(
    inventory: Mapping[str, Any], contracts: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    present = set(inventory.get("table_counts", {}).keys())
    evidence = {
        "instrument_master": {"securities", "exchange_symbols"},
        "cash_eod": {"daily_prices"},
        "financial_results": {"financial_fact_instances", "financial_metrics"},
        "shareholding": {"shareholding_patterns"},
        "corporate_actions": {"corporate_actions"},
        "filings": {"filings", "raw_documents"},
        "insider_disclosures": {"pit_disclosures"},
        "sast_disclosures": {"sast_disclosures"},
        "bulk_block_deals": {"bulk_deals", "block_deals"},
    }
    result = []
    for dataset, contract in contracts.get("datasets", {}).items():
        if not contract.get("required"):
            continue
        expected = evidence.get(dataset)
        if expected is None:
            result.append(
                make_item(
                    "COVERAGE_CONTRACT",
                    dataset,
                    "unknown",
                    "Required dataset has no certified operational coverage mapping.",
                )
            )
            continue
        missing = sorted(expected - present)
        if missing:
            result.append(
                make_item(
                    "LOCAL_DB",
                    dataset,
                    "unknown",
                    "Required source tables are absent: {}.".format(", ".join(missing)),
                )
            )
    return result


def build_items(
    inventory: Mapping[str, Any], contracts: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    for row in rows_from_csv(detail_path(inventory, "price_checkpoints")):
        status = normalize_status(row.get("status", ""))
        items.append(
            make_item(
                row.get("source", "").upper(),
                "cash_eod",
                status,
                row.get("error") or "Non-terminal daily-price checkpoint.",
                identity=row.get("checkpoint_key", ""),
                period_start=row.get("checkpoint_date", ""),
                period_end=row.get("checkpoint_date", ""),
            )
        )

    for row in rows_from_csv(detail_path(inventory, "filing_index_checkpoints")):
        source_status = row.get("status", "")
        status = "retryable" if source_status == "partial" else normalize_status(source_status)
        items.append(
            make_item(
                row.get("exchange", ""),
                row.get("dataset", ""),
                status,
                row.get("error") or "Filing-index window is not complete.",
                identity="{}:{}:{}".format(
                    row.get("exchange", ""),
                    row.get("dataset", ""),
                    row.get("window_start", ""),
                ),
                period_start=row.get("window_start", ""),
                period_end=row.get("window_end", ""),
                attempts=int(row.get("attempts") or 0),
                count=max(1, int(row.get("failed_count") or 0)),
            )
        )

    for row in rows_from_csv(detail_path(inventory, "filing_document_status")):
        status = normalize_status(row.get("status", ""))
        items.append(
            make_item(
                row.get("exchange", ""),
                row.get("dataset", ""),
                status,
                row.get("error") or "Linked source document is not complete.",
                identity=row.get("external_id", ""),
                attempts=int(row.get("attempts") or 0),
                locator=row.get("document_url", ""),
            )
        )

    for row in rows_from_csv(detail_path(inventory, "bse_financial_checkpoints")):
        source_status = row.get("status", "")
        status = "retryable" if source_status == "partial" else normalize_status(source_status)
        items.append(
            make_item(
                "BSE",
                "financial_results",
                status,
                row.get("error")
                or "BSE per-scrip financial checkpoint is not complete.",
                identity=row.get("scrip_code", ""),
                period_start=row.get("range_start", ""),
                period_end=row.get("range_end", ""),
                attempts=int(row.get("attempts") or 0),
                count=max(1, int(row.get("failed_count") or 0)),
            )
        )

    screener = inventory.get("screener", {})
    screener_detail = (
        screener.get("detail", {}).get("path")
        if isinstance(screener, Mapping)
        and isinstance(screener.get("detail"), Mapping)
        else None
    )
    for row in rows_from_csv(screener_detail):
        source_status = row.get("status", "unknown")
        mapped = {
            "pages_collected": "complete",
            "pending": "retryable",
            "partial": "partial",
            "unresolved": "unknown",
            "routed_to_fund_dataset": "not_applicable",
        }.get(source_status, "unknown")
        if mapped == "complete":
            continue
        identity = (
            row.get("key")
            or row.get("nse_symbol")
            or row.get("bse_code")
            or row.get("name")
            or ""
        )
        items.append(
            make_item(
                "SCREENER",
                "fundamentals",
                mapped,
                row.get("reason") or "Screener coverage is not complete.",
                identity=identity,
            )
        )

    items.extend(dataset_contract_items(inventory, contracts))

    rank = {
        "retryable": 0,
        "blocked": 1,
        "partial": 2,
        "unknown": 3,
        "source_unavailable": 4,
        "not_published": 5,
        "not_applicable": 6,
        "complete": 7,
    }
    dedup: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    for value in items:
        key = (
            value["source"],
            value["dataset"],
            value["identity"],
            value["period_start"],
            value["period_end"],
        )
        existing = dedup.get(key)
        if existing is None or rank.get(value["status"], 99) < rank.get(
            existing["status"], 99
        ):
            dedup[key] = value
    result = list(dedup.values())
    result.sort(
        key=lambda value: (
            value["next_action"] != "download",
            value["source"],
            value["dataset"],
            value["identity"],
            value["period_start"],
        )
    )
    return result


def summarize(items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    item_counts: Dict[str, int] = {}
    affected_counts: Dict[str, int] = {}
    for value in items:
        status = str(value["status"])
        item_counts[status] = item_counts.get(status, 0) + 1
        affected_counts[status] = affected_counts.get(status, 0) + int(
            value.get("affected_count", 1) or 1
        )
    return {
        "item_counts": dict(sorted(item_counts.items())),
        "affected_counts": dict(sorted(affected_counts.items())),
        "download_items": sum(
            1 for value in items if value.get("next_action") == "download"
        ),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--contracts", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    contracts_path = args.contracts or (
        Path(__file__).resolve().parents[1]
        / "config"
        / "aaru_dataset_contracts.json"
    )
    contracts = json.loads(contracts_path.read_text(encoding="utf-8"))
    items = build_items(inventory, contracts)
    payload = {
        "schema": "aaru.missing-manifest.v3",
        "generated_at": utcnow(),
        "inventory_schema": inventory.get("schema"),
        "summary": summarize(items),
        "items": items,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    jsonl = args.output.with_suffix(".jsonl")
    with jsonl.open("w", encoding="utf-8") as handle:
        for value in items:
            handle.write(json.dumps(value, sort_keys=True) + "\n")
    print(args.output)
    print(jsonl)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Collect official NSE equity disclosures and XBRL fundamentals."""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.config import DATABASE_URL
from market_data.database import MarketDatabase
from market_data.disclosures import (
    BSEDisclosureClient,
    BSEDisclosureCollector,
    NSEDisclosureClient,
    NSEDisclosureCollector,
)


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected YYYY-MM-DD") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Collect NSE disclosures into the shared local SQLite database."
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        choices=NSEDisclosureCollector.DATASETS,
        default=list(NSEDisclosureCollector.DATASETS),
    )
    parser.add_argument("--exchange", choices=("nse", "bse", "all"), default="nse")
    parser.add_argument(
        "--bse-scrip-code", action="append", default=None,
        help="Limit BSE enrichment to one or more numeric scrip codes.",
    )
    parser.add_argument("--start-date", type=parse_date, default=date.today() - timedelta(days=30))
    parser.add_argument("--end-date", type=parse_date, default=date.today())
    parser.add_argument("--symbol")
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--request-delay", type=float, default=0.75)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--skip-documents", action="store_true",
        help="Index filings without downloading linked XBRL/attachments.",
    )
    args = parser.parse_args(argv)
    if args.window_days < 1 or args.window_days > 90:
        parser.error("--window-days must be between 1 and 90")

    database = MarketDatabase(args.database_url)
    database.initialize()
    counts = {}
    if args.exchange in ("nse", "all"):
        client = NSEDisclosureClient(
            timeout=args.timeout, delay=args.request_delay, retries=args.retries
        )
        collector = NSEDisclosureCollector(
            database, client=client, fetch_documents=not args.skip_documents
        )
        try:
            counts["nse"] = collector.collect(
                args.start_date,
                args.end_date,
                datasets=args.datasets,
                symbol=args.symbol,
                window_days=args.window_days,
            )
        finally:
            client.session.close()
    if args.exchange in ("bse", "all"):
        bse_datasets = [
            item for item in args.datasets if item in BSEDisclosureCollector.DATASETS
        ]
        bse_client = BSEDisclosureClient(
            timeout=args.timeout, delay=args.request_delay
        )
        try:
            counts["bse"] = BSEDisclosureCollector(
                database, bse_client
            ).collect(
                bse_datasets,
                args.bse_scrip_code,
                start=args.start_date,
                end=args.end_date,
            )
        finally:
            bse_client.session.close()
    print(json.dumps(counts, sort_keys=True))
    failed = sum(
        value
        for exchange_counts in counts.values()
        for key, value in exchange_counts.items()
        if key.endswith("_failed")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

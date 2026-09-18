#!/usr/bin/env python3
"""Backfill official NSE XBRL fundamentals and report database coverage."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.config import DATABASE_URL
from market_data.database import MarketDatabase
from market_data.disclosures import BSEDisclosureClient, NSEDisclosureClient
from market_data.fundamental_backfill import (
    BACKFILL_DATASETS,
    BSEFundamentalBackfill,
    FundamentalBackfill,
    coverage_report,
    dumps_report,
    format_coverage,
)


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected YYYY-MM-DD") from exc


def _shared_database_argument(parser):
    parser.add_argument("--database-url", default=DATABASE_URL)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Resumable official NSE financial/shareholding XBRL history backfill."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backfill = subparsers.add_parser(
        "backfill", help="Backfill bulk NSE filing indexes and linked XBRL."
    )
    _shared_database_argument(backfill)
    backfill.add_argument("--start-date", type=parse_date, required=True)
    backfill.add_argument("--end-date", type=parse_date, default=date.today())
    backfill.add_argument(
        "--datasets", nargs="+", choices=BACKFILL_DATASETS,
        default=list(BACKFILL_DATASETS),
    )
    backfill.add_argument(
        "--exchange", choices=("nse", "bse", "all"), default="all",
        help="NSE uses bulk windows; BSE financial history uses per-scrip indexes.",
    )
    backfill.add_argument(
        "--bse-scrip-code", action="append",
        help="Limit BSE history to one or more numeric scrip codes.",
    )
    backfill.add_argument("--window-days", type=int, default=30)
    backfill.add_argument("--workers", type=int, default=3)
    backfill.add_argument("--timeout", type=float, default=30.0)
    backfill.add_argument("--request-delay", type=float, default=0.75)
    backfill.add_argument("--retries", type=int, default=3)
    backfill.add_argument(
        "--no-resume", action="store_true",
        help="Process completed windows again (cached documents are still reused).",
    )
    backfill.add_argument(
        "--retry-failed", action="store_true",
        help="Retry only failed, partial, interrupted, or previously unseen windows.",
    )
    backfill.add_argument(
        "--refresh-documents", action="store_true",
        help="Redownload linked documents even when a content-addressed copy exists.",
    )

    report = subparsers.add_parser(
        "report", help="Report financial, ownership, and document coverage."
    )
    _shared_database_argument(report)
    report.add_argument("--json", action="store_true", help="Emit JSON.")
    report.add_argument("--latest-limit", type=int, default=20)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    database = MarketDatabase(args.database_url)
    database.initialize()
    if args.command == "report":
        report = coverage_report(database, latest_limit=args.latest_limit)
        print(dumps_report(report) if args.json else format_coverage(report))
        return 0

    if args.workers < 1 or args.workers > 16:
        parser.error("--workers must be between 1 and 16")
    if args.window_days < 1 or args.window_days > 90:
        parser.error("--window-days must be between 1 and 90")
    if args.request_delay < 0:
        parser.error("--request-delay must not be negative")

    summary = {}
    if args.exchange == "bse" and "financial_results" not in args.datasets:
        parser.error("BSE backfill supports only financial_results")
    if args.exchange in ("nse", "all"):
        index_client = NSEDisclosureClient(
            timeout=args.timeout,
            delay=args.request_delay,
            retries=args.retries,
        )

        def nse_document_client():
            return NSEDisclosureClient(
                timeout=args.timeout,
                delay=0,
                retries=args.retries,
            )

        try:
            summary["nse"] = FundamentalBackfill(
                database,
                index_client,
                document_client_factory=nse_document_client,
                workers=args.workers,
                download_interval=args.request_delay,
                output=sys.stderr,
            ).run(
                args.start_date,
                args.end_date,
                datasets=args.datasets,
                window_days=args.window_days,
                resume=not args.no_resume,
                retry_failed=args.retry_failed,
                refresh_documents=args.refresh_documents,
            )
        finally:
            index_client.session.close()
    if (
        args.exchange in ("bse", "all")
        and "financial_results" in args.datasets
        and not summary.get("nse", {}).get("interrupted")
    ):
        bse_client = BSEDisclosureClient(
            timeout=args.timeout, delay=args.request_delay
        )

        def bse_document_client():
            return BSEDisclosureClient(timeout=args.timeout, delay=args.request_delay)

        try:
            summary["bse"] = BSEFundamentalBackfill(
                database,
                bse_client,
                document_client_factory=bse_document_client,
                workers=args.workers,
                output=sys.stderr,
            ).run(
                args.start_date,
                args.end_date,
                scrip_codes=args.bse_scrip_code,
                resume=not args.no_resume,
                retry_failed=args.retry_failed,
                refresh_documents=args.refresh_documents,
            )
        finally:
            bse_client.session.close()
    print(json.dumps(summary, sort_keys=True))
    if any(result["interrupted"] for result in summary.values()):
        return 130
    failed = sum(
        result.get("windows_failed", 0) + result.get("scrips_failed", 0)
        for result in summary.values()
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

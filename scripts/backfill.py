#!/usr/bin/env python3
"""Maximum-history official NSE/BSE backfill and coverage CLI."""

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.config import API_RETRY_ATTEMPTS, API_TIMEOUT, DATABASE_URL, REQUEST_DELAY
from market_data.backfill import (
    ConcurrentArchiveFetcher,
    HistoricalBackfill,
    PUBLIC_BOUNDARIES,
    coverage_report,
)
from market_data.calendar import last_completed_trading_day
from market_data.database import MarketDatabase
from market_data.http import ExchangeHTTPClient, HostRateLimiter
from market_data.sources import BSESource, NSESource


logger = logging.getLogger("market_data.backfill")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected YYYY-MM-DD: {}".format(value)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("backfill", "coverage"), default="backfill")
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument("--exchange", choices=("nse", "bse", "all"), default="all")
    parser.add_argument("--start-date", type=parse_date, help="Override both exchange boundaries.")
    parser.add_argument("--nse-start", type=parse_date, default=PUBLIC_BOUNDARIES["nse"])
    parser.add_argument("--bse-start", type=parse_date, default=PUBLIC_BOUNDARIES["bse"])
    parser.add_argument("--end-date", type=parse_date, default=last_completed_trading_day())
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--request-delay",
        type=float,
        default=max(1.0, REQUEST_DELAY),
        help="Minimum seconds between request starts per host (default: 1.0).",
    )
    parser.add_argument("--circuit-cooldown", type=float, default=300.0)
    parser.add_argument("--timeout", type=float, default=API_TIMEOUT)
    parser.add_argument("--retries", type=int, default=API_RETRY_ATTEMPTS)
    parser.add_argument("--retry-failed", action="store_true", help="Process only recorded failures.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable coverage JSON.")
    parser.add_argument("--progress-interval", type=float, default=30.0)
    parser.add_argument("--verbose", action="store_true")
    return parser


def _sources(exchange: str) -> List[str]:
    return ["nse", "bse"] if exchange == "all" else [exchange]


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _print_coverage(reports, as_json: bool) -> None:
    if as_json:
        print(json.dumps(reports, default=_json_default, indent=2, sort_keys=True))
        return
    for report in reports:
        print(
            "{exchange}: {earliest}..{latest} | expected={expected_days} "
            "present={present_days} missing={missing_days} rows={rows} "
            "securities={distinct_securities} updated={last_update}".format(**report)
        )
        counts = ", ".join(
            "{}={}".format(key, value)
            for key, value in sorted(report["status_counts"].items())
        )
        print("  checkpoints: {}".format(counts or "none"))
        if report["missing_dates"]:
            preview = ", ".join(item.isoformat() for item in report["missing_dates"][:20])
            suffix = " ..." if len(report["missing_dates"]) > 20 else ""
            print("  missing: {}{}".format(preview, suffix))


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.workers < 1 or args.workers > 16:
        raise SystemExit("--workers must be between 1 and 16")
    if args.end_date > date.today():
        raise SystemExit("--end-date cannot be in the future")
    sources = _sources(args.exchange)
    starts: Dict[str, date] = {
        "nse": args.start_date or args.nse_start,
        "bse": args.start_date or args.bse_start,
    }
    database = MarketDatabase(args.database_url)
    database.initialize()
    if args.command == "coverage":
        _print_coverage(coverage_report(database, sources, starts, args.end_date), args.json)
        return 0

    limiter = HostRateLimiter(args.request_delay, cooldown=args.circuit_cooldown)

    def source_factory(source: str):
        client = ExchangeHTTPClient(
            timeout=args.timeout,
            delay=0,
            retries=args.retries,
            rate_limiter=limiter,
        )
        return NSESource(client) if source == "nse" else BSESource(client)

    fetcher = ConcurrentArchiveFetcher(source_factory)
    service = HistoricalBackfill(
        database,
        fetcher,
        workers=args.workers,
        progress_interval=args.progress_interval,
    )
    plan = service.plan(
        sources,
        starts,
        args.end_date,
        only_failures=args.retry_failed,
    )
    logger.info(
        "planned %d archive dates (%s through %s) with %d workers%s",
        len(plan),
        min((item[1] for item in plan), default="-"),
        max((item[1] for item in plan), default="-"),
        args.workers,
        " [dry run]" if args.dry_run else "",
    )
    try:
        result = service.run(plan, args.end_date, dry_run=args.dry_run)
    finally:
        fetcher.close()
    logger.info(
        "complete attempted=%d succeeded=%d unavailable=%d failed=%d rows=%d",
        result.attempted,
        result.succeeded,
        result.unavailable,
        result.failed,
        result.rows_written,
    )
    if args.dry_run:
        _print_coverage(coverage_report(database, sources, starts, args.end_date), args.json)
    return 130 if result.interrupted else (1 if result.failed else 0)


if __name__ == "__main__":
    sys.exit(main())

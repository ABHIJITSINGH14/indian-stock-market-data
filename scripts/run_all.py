#!/usr/bin/env python3
"""CLI for the durable NSE/BSE SQLite ingestion pipeline."""

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.config import (  # noqa: E402
    API_RETRY_ATTEMPTS,
    API_TIMEOUT,
    DATABASE_URL,
    REQUEST_DELAY,
)
from market_data.database import MarketDatabase  # noqa: E402
from market_data.http import ExchangeHTTPClient  # noqa: E402
from market_data.ingestion import IngestionResult, IngestionService  # noqa: E402
from market_data.sources import BSESource, NSESource  # noqa: E402


logger = logging.getLogger("market_data")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected YYYY-MM-DD: {}".format(value)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize and ingest official NSE/BSE equity market data."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=("init-db", "masters", "prices", "all"),
    )
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument(
        "--exchange",
        choices=("nse", "bse", "all"),
        default="all",
        help="Limit ingestion to one exchange.",
    )
    parser.add_argument("--start-date", type=parse_date)
    parser.add_argument("--end-date", type=parse_date, default=date.today())
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry previously failed dates through --end-date.",
    )
    parser.add_argument("--timeout", type=float, default=API_TIMEOUT)
    parser.add_argument("--request-delay", type=float, default=REQUEST_DELAY)
    parser.add_argument("--retries", type=int, default=API_RETRY_ATTEMPTS)
    parser.add_argument("--verbose", action="store_true")
    return parser


def _sources(exchange: str, client: ExchangeHTTPClient):
    available = {"nse": NSESource(client), "bse": BSESource(client)}
    if exchange == "all":
        return list(available.values())
    return [available[exchange]]


def _incremental_start(
    database: MarketDatabase, source_name: str, requested: Optional[date], end: date
) -> date:
    if requested:
        return requested
    latest = database.latest_successful_date(source_name, "daily_prices")
    return latest + timedelta(days=1) if latest else end


def _log_result(result: IngestionResult) -> None:
    logger.info(
        "%s %s: attempted=%d succeeded=%d failed=%d rows=%d",
        result.source.upper(),
        result.dataset,
        result.attempted,
        result.succeeded,
        result.failed,
        result.rows_written,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database = MarketDatabase(args.database_url)
    database.initialize()
    logger.info("SQLite schema ready at %s", args.database_url)
    if args.command == "init-db":
        return 0

    client = ExchangeHTTPClient(
        timeout=args.timeout,
        delay=args.request_delay,
        retries=args.retries,
    )
    service = IngestionService(database)
    results = []
    try:
        selected_sources = _sources(args.exchange, client)
        if args.command in {"masters", "all"}:
            results.extend(service.ingest_master(source) for source in selected_sources)
        if args.command in {"prices", "all"}:
            for source in selected_sources:
                start = _incremental_start(
                    database, source.name, args.start_date, args.end_date
                )
                if start > args.end_date:
                    logger.info("%s daily prices are already current", source.name.upper())
                    continue
                results.append(
                    service.ingest_prices(
                        source, start, args.end_date, retry_failed=args.retry_failed
                    )
                )
    finally:
        client.close()

    for result in results:
        _log_result(result)
    return 0 if all(result.failed == 0 for result in results) else 1


def run_all_downloads() -> bool:
    """Backward-compatible entry point used by the package console script."""
    return main(["all"]) == 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Backfill official CDSL historical FII/FPI market activity."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.config import DATABASE_URL
from market_data.database import MarketDatabase
from market_data.institutional_backfill import (
    CDSLInstitutionalClient,
    InstitutionalBackfill,
)


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected YYYY-MM-DD") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument("--start-date", type=parse_date, default=date(1999, 1, 1))
    parser.add_argument("--end-date", type=parse_date, default=date.today())
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--request-delay", type=float, default=1)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    if args.start_date > args.end_date:
        parser.error("--start-date cannot be after --end-date")
    database = MarketDatabase(args.database_url)
    database.initialize()
    client = CDSLInstitutionalClient(
        timeout=args.timeout,
        delay=args.request_delay,
    )
    service = InstitutionalBackfill(database, client)
    try:
        summary = service.run(
            service.plan(args.start_date, args.end_date, args.retry_failed)
        )
    finally:
        client.close()
    print(json.dumps(summary, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())

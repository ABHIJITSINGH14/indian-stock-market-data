#!/usr/bin/env python3
"""Run safe fundamental screens against the local SQLite database."""

import argparse
import json
import sqlite3
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.config import DATABASE_PATH
from fundamentals import (
    Condition, Operator, ScreenRequest, Screener, Sort,
    SQLiteReadSchema, SQLiteSnapshotAdapter,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Screen locally collected Indian equities.")
    parser.add_argument("--database", default=DATABASE_PATH)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument(
        "--where", action="append", default=[], metavar="FIELD:OP:VALUE",
        help="Example: roe:gte:15 or debt_equity:lt:1",
    )
    parser.add_argument("--sort", action="append", default=[], metavar="FIELD[:desc]")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    conditions = []
    for item in args.where:
        parts = item.split(":")
        if len(parts) not in (2, 3):
            parser.error("Invalid --where {!r}".format(item))
        field, operator = parts[:2]
        value = Decimal(parts[2]) if len(parts) == 3 else None
        conditions.append(Condition(field, Operator(operator), value))
    sorts = []
    for item in args.sort:
        parts = item.split(":")
        sorts.append(Sort(parts[0], len(parts) > 1 and parts[1].lower() == "desc"))

    connection = sqlite3.connect(args.database)
    schema = SQLiteReadSchema(
        metric_table="financial_metrics",
        market_table="market_metrics",
        metric_columns={
            "symbol": "symbol", "filing_date": "filing_date",
            "period_end": "period_end", "metric": "metric", "value": "value",
        },
        market_columns={
            "symbol": "symbol", "as_of": "as_of", "metric": "metric", "value": "value",
        },
    )
    results = Screener(SQLiteSnapshotAdapter(connection, schema)).run(
        ScreenRequest(
            as_of=args.as_of,
            conditions=tuple(conditions),
            sort=tuple(sorts),
            limit=args.limit,
        )
    )
    print(json.dumps([
        {
            "symbol": item.symbol,
            "period_end": item.period_end.isoformat(),
            "values": {
                key: str(value) if value is not None else None
                for key, value in item.values.items()
            },
        }
        for item in results
    ], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())


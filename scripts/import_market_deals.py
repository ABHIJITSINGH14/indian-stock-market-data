#!/usr/bin/env python3
"""Import sealed official NSE bulk and block deals into canonical SQLite."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.database import MarketDatabase
from market_data.deal_import import import_sealed_deals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--source-database", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--checksum", type=Path)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()

    database = MarketDatabase(args.database_url)
    try:
        result = import_sealed_deals(
            database,
            args.source_database,
            args.manifest,
            args.checksum,
            args.batch_size,
        )
        print(json.dumps(result, sort_keys=True))
    finally:
        database.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

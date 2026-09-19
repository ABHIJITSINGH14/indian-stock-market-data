#!/usr/bin/env python3
"""Losslessly compress cached exchange documents in the local database."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.database import MarketDatabase
from market_data.disclosures import DisclosureStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()

    database = MarketDatabase(args.database_url)
    database.initialize()
    store = DisclosureStore(database)
    store.initialize()
    try:
        result = store.compress_documents(args.batch_size)
        print(json.dumps(result, sort_keys=True))
    finally:
        database.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

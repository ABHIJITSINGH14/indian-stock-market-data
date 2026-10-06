#!/usr/bin/env python3
"""Register a sealed local market-data catalog in the canonical database."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.database import MarketDatabase
from market_data.federation import import_sealed_catalog


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    database = MarketDatabase(args.database_url)
    try:
        result = import_sealed_catalog(database, args.manifest)
        print(json.dumps(result, sort_keys=True))
    finally:
        database.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

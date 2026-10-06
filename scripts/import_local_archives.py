#!/usr/bin/env python3
"""Import checksum-manifested official bhavcopies already present on disk."""

import argparse
from datetime import date
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data.database import MarketDatabase
from market_data.local_archive import (
    GIB,
    LocalArchiveImporter,
    load_external_manifest,
    load_manifest,
)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected YYYY-MM-DD") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument(
        "--exchange", choices=("nse", "bse", "all"), default="all"
    )
    parser.add_argument("--min-free-gib", type=float, default=12.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--start-date", type=parse_date)
    parser.add_argument("--end-date", type=parse_date)
    parser.add_argument("--catalog-only", action="store_true")
    parser.add_argument(
        "--catalog-all",
        action="store_true",
        help="Verify and register every manifested asset kind without materializing.",
    )
    parser.add_argument(
        "--compress-kind",
        action="append",
        help="Losslessly gzip and catalog a completed manifested asset kind.",
    )
    args = parser.parse_args()

    kinds = (
        ("nse_cm", "bse_cm")
        if args.exchange == "all"
        else ("{}_cm".format(args.exchange),)
    )
    database = MarketDatabase(args.database_url)
    try:
        importer = LocalArchiveImporter(
            database, min_free_bytes=int(args.min_free_gib * GIB)
        )
        if args.compress_kind:
            if (
                args.catalog_all
                or args.catalog_only
                or args.start_date
                or args.end_date
                or args.limit
            ):
                parser.error(
                    "--compress-kind cannot be combined with catalog/import filters"
                )
            selected = tuple(dict.fromkeys(args.compress_kind))
            result = dict(
                importer.compress_external_entries(
                    load_external_manifest(args.manifest, args.raw_root, selected)
                )
            )
            result.update(
                importer.catalog_external_entries(
                    load_external_manifest(args.manifest, args.raw_root, selected)
                )
            )
        elif args.catalog_all:
            if args.start_date or args.end_date or args.limit:
                parser.error(
                    "--catalog-all cannot be combined with date filters or --limit"
                )
            result = importer.catalog_external_entries(
                load_external_manifest(args.manifest, args.raw_root)
            )
        else:
            entries = load_manifest(args.manifest, args.raw_root, kinds)
            entries = [
                entry
                for entry in entries
                if (args.start_date is None or entry.trading_date >= args.start_date)
                and (args.end_date is None or entry.trading_date <= args.end_date)
            ]
            result = (
                importer.catalog_entries(entries)
                if args.catalog_only
                else importer.import_entries(entries, limit=args.limit)
            )
        print(json.dumps(result, sort_keys=True))
        return 75 if result.get("status") == "low_disk" else 0
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

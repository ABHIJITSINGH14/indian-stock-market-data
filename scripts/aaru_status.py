#!/usr/bin/env python3
"""Fast status summary that does not scan or lock the live SQLite database."""

from __future__ import annotations
import argparse, json, shutil
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--database", required=True, type=Path)
    p.add_argument("--state-file", type=Path)
    p.add_argument("--screener-summary", type=Path)
    args = p.parse_args()
    db = args.database.expanduser().resolve()
    storage = shutil.disk_usage(db.parent)
    payload = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "path": str(db),
            "exists": db.exists(),
            "bytes": db.stat().st_size if db.exists() else None,
            "wal_bytes": Path(str(db) + "-wal").stat().st_size
            if Path(str(db) + "-wal").exists()
            else 0,
            "modified_at": datetime.fromtimestamp(
                db.stat().st_mtime, timezone.utc
            ).isoformat()
            if db.exists()
            else None,
        },
        "filesystem": {
            "total_bytes": storage.total,
            "used_bytes": storage.used,
            "free_bytes": storage.free,
        },
        "auto_backfill_state": None,
        "screener": None,
    }
    if args.state_file and args.state_file.exists():
        payload["auto_backfill_state"] = json.loads(
            args.state_file.read_text(encoding="utf-8")
        )
    if args.screener_summary and args.screener_summary.exists():
        payload["screener"] = json.loads(
            args.screener_summary.read_text(encoding="utf-8")
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

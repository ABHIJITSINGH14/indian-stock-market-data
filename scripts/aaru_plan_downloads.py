#!/usr/bin/env python3
"""Compile a finite AARU missing manifest into non-overlapping download jobs.

The planner never executes network collectors. It emits an auditable JSON plan
and a sequential shell script. All jobs writing stock_market.db share one mutex
and must not run alongside auto_backfill.py or another SQLite writer.
"""

from __future__ import annotations

import argparse
import json
import shlex
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def quote(value: str) -> str:
    return shlex.quote(str(value))


def date_bounds(items: Sequence[Mapping[str, Any]], default_start: str, default_end: str) -> Tuple[str, str]:
    starts = sorted(str(item.get("period_start") or "") for item in items if item.get("period_start"))
    ends = sorted(str(item.get("period_end") or "") for item in items if item.get("period_end"))
    return (starts[0] if starts else default_start, ends[-1] if ends else default_end)


def build_plan(
    manifest: Mapping[str, Any],
    *,
    database_url: str,
    python: str = "python3",
    default_start: str = "2007-02-01",
    default_end: Optional[str] = None,
    screener_command: str = "",
    reserve_gib: float = 12.0,
) -> Dict[str, Any]:
    default_end = default_end or date.today().isoformat()
    retryable = [
        item for item in manifest.get("items", [])
        if item.get("status") == "retryable" and item.get("next_action") == "download"
    ]
    grouped: Dict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for item in retryable:
        grouped[(str(item.get("source", "")).upper(), str(item.get("dataset", "")))].append(item)

    jobs: List[Dict[str, Any]] = []
    unsupported: List[Dict[str, Any]] = []

    for (source, dataset), items in sorted(grouped.items()):
        start, end = date_bounds(items, default_start, default_end)
        command: List[str] = []
        writer = False
        collector = ""

        if source in {"NSE", "BSE"} and dataset == "cash_eod":
            collector = "official_price_backfill"
            writer = True
            command = [
                python, "scripts/backfill.py", "backfill",
                "--database-url", database_url,
                "--exchange", source.lower(),
                "--retry-failed",
                "--end-date", end,
                "--workers", "2" if source == "NSE" else "1",
                "--request-delay", "2" if source == "NSE" else "4",
                "--timeout", "45", "--retries", "4",
            ]
        elif source in {"NSE", "BSE"} and dataset in {"financial_results", "shareholding"}:
            collector = "official_fundamental_backfill"
            writer = True
            datasets = sorted({str(item.get("dataset")) for item in items})
            command = [
                python, "scripts/backfill_fundamentals.py", "backfill",
                "--database-url", database_url,
                "--exchange", source.lower(),
                "--start-date", start,
                "--end-date", end,
                "--datasets", *datasets,
                "--retry-failed",
                "--workers", "2" if source == "NSE" else "1",
                "--request-delay", "2" if source == "NSE" else "4",
                "--timeout", "45", "--retries", "4",
            ]
        elif source == "SCREENER" and dataset == "fundamentals" and screener_command:
            collector = "screener_resume_v3"
            command = [screener_command]
        else:
            unsupported.append({
                "source": source,
                "dataset": dataset,
                "items": len(items),
                "affected_count": sum(int(item.get("affected_count", 1) or 1) for item in items),
                "reason": "No certified collector mapping exists; implement or review before execution.",
            })
            continue

        jobs.append({
            "id": "{}-{}".format(source.lower(), dataset.replace("_", "-")),
            "source": source,
            "dataset": dataset,
            "collector": collector,
            "manifest_items": len(items),
            "affected_count": sum(int(item.get("affected_count", 1) or 1) for item in items),
            "period_start": start,
            "period_end": end,
            "writes_database": writer,
            "mutex": "stock_market_db_writer" if writer else None,
            "requires_auto_backfill_stopped": writer,
            "command": command,
            "shell": " ".join(quote(part) for part in command),
        })

    return {
        "schema": "aaru.download-plan.v1",
        "generated_at": utcnow(),
        "manifest_schema": manifest.get("schema"),
        "database_url": database_url,
        "reserve_gib": reserve_gib,
        "execution_policy": {
            "sqlite_writers_sequential": True,
            "auto_backfill_must_be_stopped_for_writer_jobs": True,
            "terminal_gaps_not_retried": True,
        },
        "jobs": jobs,
        "unsupported": unsupported,
    }


def render_shell(plan: Mapping[str, Any]) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# Generated by scripts/aaru_plan_downloads.py.",
        "# Stop auto_backfill.py before running database-writer jobs.",
        "# Execute sequentially; do not parallelize SQLite writers.",
        "",
        "MIN_FREE_GIB={}".format(plan.get("reserve_gib", 12.0)),
        "check_space() {",
        "  python3 - \"$MIN_FREE_GIB\" <<'PY'",
        "import os, sys",
        "need=float(sys.argv[1])",
        "s=os.statvfs('.')",
        "free=s.f_bavail*s.f_frsize/1024**3",
        "print(f'free_gib={free:.2f} required_gib={need:.2f}')",
        "raise SystemExit(0 if free >= need else 75)",
        "PY",
        "}",
        "",
    ]
    for job in plan.get("jobs", []):
        lines += [
            "echo '=== {} ==='".format(job["id"]),
            "check_space",
            job["shell"],
            "",
        ]
    if plan.get("unsupported"):
        lines.append("# Unsupported manifest families requiring a collector/review:")
        for item in plan["unsupported"]:
            lines.append("# - {source}/{dataset}: {items} items".format(**item))
    return "\n".join(lines) + "\n"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--python", default="python3")
    parser.add_argument("--default-start", default="2007-02-01")
    parser.add_argument("--default-end", default=date.today().isoformat())
    parser.add_argument("--screener-command", default="")
    parser.add_argument("--reserve-gib", type=float, default=12.0)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    plan = build_plan(
        manifest,
        database_url=args.database_url,
        python=args.python,
        default_start=args.default_start,
        default_end=args.default_end,
        screener_command=args.screener_command,
        reserve_gib=args.reserve_gib,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    shell_path = args.output.with_suffix(".sh")
    shell_path.write_text(render_shell(plan), encoding="utf-8")
    shell_path.chmod(0o755)
    print(args.output)
    print(shell_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

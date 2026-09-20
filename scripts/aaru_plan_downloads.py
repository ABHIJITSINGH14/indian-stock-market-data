#!/usr/bin/env python3
"""Compile a finite AARU missing manifest into non-overlapping download jobs.

The planner never executes network collectors. It emits an auditable JSON plan
and a sequential shell script. All jobs writing stock_market.db share one mutex
and must not run alongside auto_backfill.py or another SQLite writer.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def quote(value: str) -> str:
    return shlex.quote(str(value))


def date_bounds(
    items: Sequence[Mapping[str, Any]],
    default_start: str,
    default_end: str,
) -> Tuple[str, str]:
    starts = sorted(
        str(item.get("period_start") or "")
        for item in items
        if item.get("period_start")
    )
    ends = sorted(
        str(item.get("period_end") or "")
        for item in items
        if item.get("period_end")
    )
    return (
        starts[0] if starts else default_start,
        ends[-1] if ends else default_end,
    )


def affected_count(items: Iterable[Mapping[str, Any]]) -> int:
    return sum(int(item.get("affected_count", 1) or 1) for item in items)


def bse_scrip_codes(items: Sequence[Mapping[str, Any]]) -> List[str]:
    """Extract numeric BSE scrip codes from item identities/locators.

    Known identities include bare codes and external IDs such as:
    `BSE:500325:...`.
    """
    values = set()
    for item in items:
        for field in ("identity", "locator"):
            value = str(item.get(field) or "")
            if value.isdigit() and 5 <= len(value) <= 8:
                values.add(value)
                continue
            match = re.search(r"(?:^|:)BSE:(\d{5,8})(?::|$)", value, re.I)
            if match:
                values.add(match.group(1))
                continue
            match = re.search(r"(?:^|/)(\d{6})(?:/|$)", value)
            if match:
                values.add(match.group(1))
    return sorted(values)


def _job(
    *,
    job_id: str,
    source: str,
    dataset: str,
    collector: str,
    items: Sequence[Mapping[str, Any]],
    start: str,
    end: str,
    command: Sequence[str],
    writes_database: bool,
    broad_scope: bool = False,
    notes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    return {
        "id": job_id,
        "source": source,
        "dataset": dataset,
        "collector": collector,
        "manifest_items": len(items),
        "affected_count": affected_count(items),
        "period_start": start,
        "period_end": end,
        "writes_database": writes_database,
        "mutex": "stock_market_db_writer" if writes_database else None,
        "requires_auto_backfill_stopped": writes_database,
        "broad_scope": broad_scope,
        "notes": list(notes or ()),
        "command": list(command),
        "shell": " ".join(quote(part) for part in command),
    }


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
        item
        for item in manifest.get("items", [])
        if item.get("status") == "retryable"
        and item.get("next_action") == "download"
    ]

    by_source_dataset: Dict[
        Tuple[str, str], List[Mapping[str, Any]]
    ] = defaultdict(list)
    for item in retryable:
        by_source_dataset[
            (
                str(item.get("source", "")).upper(),
                str(item.get("dataset", "")),
            )
        ].append(item)

    jobs: List[Dict[str, Any]] = []
    unsupported: List[Dict[str, Any]] = []

    # Prices: one bounded retry job per exchange.
    for source in ("NSE", "BSE"):
        items = by_source_dataset.pop((source, "cash_eod"), [])
        if not items:
            continue
        start, end = date_bounds(items, default_start, default_end)
        command = [
            python,
            "scripts/backfill.py",
            "backfill",
            "--database-url",
            database_url,
            "--exchange",
            source.lower(),
            "--retry-failed",
            "--start-date",
            start,
            "--end-date",
            end,
            "--workers",
            "2" if source == "NSE" else "1",
            "--request-delay",
            "2" if source == "NSE" else "4",
            "--timeout",
            "45",
            "--retries",
            "4",
        ]
        jobs.append(
            _job(
                job_id="{}-cash-eod".format(source.lower()),
                source=source,
                dataset="cash_eod",
                collector="official_price_backfill",
                items=items,
                start=start,
                end=end,
                command=command,
                writes_database=True,
            )
        )

    # NSE fundamentals: merge result + ownership windows into one bulk-window job.
    nse_fundamental_items: List[Mapping[str, Any]] = []
    nse_datasets: List[str] = []
    for dataset in ("financial_results", "shareholding"):
        items = by_source_dataset.pop(("NSE", dataset), [])
        if items:
            nse_fundamental_items.extend(items)
            nse_datasets.append(dataset)
    if nse_fundamental_items:
        start, end = date_bounds(
            nse_fundamental_items, default_start, default_end
        )
        command = [
            python,
            "scripts/backfill_fundamentals.py",
            "backfill",
            "--database-url",
            database_url,
            "--exchange",
            "nse",
            "--start-date",
            start,
            "--end-date",
            end,
            "--datasets",
            *sorted(set(nse_datasets)),
            "--retry-failed",
            "--workers",
            "2",
            "--request-delay",
            "2",
            "--timeout",
            "45",
            "--retries",
            "4",
        ]
        jobs.append(
            _job(
                job_id="nse-fundamentals",
                source="NSE",
                dataset="+".join(sorted(set(nse_datasets))),
                collector="official_fundamental_backfill",
                items=nse_fundamental_items,
                start=start,
                end=end,
                command=command,
                writes_database=True,
            )
        )

    # BSE official backfill supports financial results only. Restrict by scrip
    # code whenever the finite manifest contains resolvable identities.
    bse_financial_items = by_source_dataset.pop(
        ("BSE", "financial_results"), []
    )
    if bse_financial_items:
        start, end = date_bounds(
            bse_financial_items, default_start, default_end
        )
        codes = bse_scrip_codes(bse_financial_items)
        command = [
            python,
            "scripts/backfill_fundamentals.py",
            "backfill",
            "--database-url",
            database_url,
            "--exchange",
            "bse",
            "--start-date",
            start,
            "--end-date",
            end,
            "--datasets",
            "financial_results",
            "--retry-failed",
            "--workers",
            "1",
            "--request-delay",
            "4",
            "--timeout",
            "45",
            "--retries",
            "4",
        ]
        for code in codes:
            command += ["--bse-scrip-code", code]
        jobs.append(
            _job(
                job_id="bse-financial-results",
                source="BSE",
                dataset="financial_results",
                collector="official_fundamental_backfill",
                items=bse_financial_items,
                start=start,
                end=end,
                command=command,
                writes_database=True,
                broad_scope=not bool(codes),
                notes=(
                    [
                        "No BSE scrip codes were resolved; retry-failed may "
                        "scan all eligible BSE scrip checkpoints."
                    ]
                    if not codes
                    else [
                        "Restricted to {} BSE scrip codes from the manifest."
                        .format(len(codes))
                    ]
                ),
            )
        )

    # BSE shareholding has no certified public collector in the branch.
    bse_shareholding = by_source_dataset.pop(("BSE", "shareholding"), [])
    if bse_shareholding:
        unsupported.append(
            {
                "source": "BSE",
                "dataset": "shareholding",
                "items": len(bse_shareholding),
                "affected_count": affected_count(bse_shareholding),
                "reason": (
                    "No certified BSE historical shareholding collector exists; "
                    "classify public-source limits or implement a permitted source."
                ),
            }
        )

    screener_items = by_source_dataset.pop(
        ("SCREENER", "fundamentals"), []
    )
    if screener_items:
        if screener_command:
            start, end = date_bounds(
                screener_items, default_start, default_end
            )
            command = [screener_command]
            jobs.append(
                _job(
                    job_id="screener-fundamentals",
                    source="SCREENER",
                    dataset="fundamentals",
                    collector="screener_resume_v3",
                    items=screener_items,
                    start=start,
                    end=end,
                    command=command,
                    writes_database=False,
                )
            )
        else:
            unsupported.append(
                {
                    "source": "SCREENER",
                    "dataset": "fundamentals",
                    "items": len(screener_items),
                    "affected_count": affected_count(screener_items),
                    "reason": (
                        "Screener retryable gaps exist but no executable "
                        "screener command was supplied."
                    ),
                }
            )

    # Anything not explicitly mapped remains review-only.
    for (source, dataset), items in sorted(by_source_dataset.items()):
        unsupported.append(
            {
                "source": source,
                "dataset": dataset,
                "items": len(items),
                "affected_count": affected_count(items),
                "reason": (
                    "No certified collector mapping exists; implement or "
                    "review before execution."
                ),
            }
        )

    return {
        "schema": "aaru.download-plan.v2",
        "generated_at": utcnow(),
        "manifest_schema": manifest.get("schema"),
        "database_url": database_url,
        "reserve_gib": reserve_gib,
        "execution_policy": {
            "sqlite_writers_sequential": True,
            "auto_backfill_must_be_stopped_for_writer_jobs": True,
            "terminal_gaps_not_retried": True,
            "resume_from_execution_state": True,
            "broad_scope_jobs_require_explicit_approval": True,
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
        "# Prefer scripts/aaru_execute_plan.py for lock/state enforcement.",
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
        if job.get("broad_scope"):
            lines.append(
                "# BROAD-SCOPE job: review before execution: {}".format(
                    job["id"]
                )
            )
        lines += [
            "echo '=== {} ==='".format(job["id"]),
            "check_space",
            job["shell"],
            "",
        ]
    if plan.get("unsupported"):
        lines.append(
            "# Unsupported manifest families requiring a collector/review:"
        )
        for value in plan["unsupported"]:
            lines.append(
                "# - {source}/{dataset}: {items} items".format(**value)
            )
    return "\n".join(lines) + "\n"


def parse_args(
    argv: Optional[Sequence[str]] = None,
) -> argparse.Namespace:
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

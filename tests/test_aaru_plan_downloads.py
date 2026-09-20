import json
from pathlib import Path

from scripts.aaru_plan_downloads import build_plan, render_shell


def test_plan_groups_retryable_items_and_separates_unsupported() -> None:
    manifest = {
        "schema": "aaru.missing-manifest.v3",
        "items": [
            {
                "source": "NSE",
                "dataset": "cash_eod",
                "status": "retryable",
                "next_action": "download",
                "period_start": "2026-09-17",
                "period_end": "2026-09-17",
                "affected_count": 1,
            },
            {
                "source": "BSE",
                "dataset": "financial_results",
                "status": "retryable",
                "next_action": "download",
                "period_start": "2018-01-01",
                "period_end": "2026-09-18",
                "affected_count": 3,
            },
            {
                "source": "SCREENER",
                "dataset": "fundamentals",
                "status": "retryable",
                "next_action": "download",
                "affected_count": 2,
            },
            {
                "source": "NSE",
                "dataset": "bulk_block_deals",
                "status": "unknown",
                "next_action": "review",
                "affected_count": 1,
            },
        ],
    }
    plan = build_plan(
        manifest,
        database_url="sqlite:///data/databases/stock_market.db",
        screener_command="/tmp/AARU_Resume_Screener.command",
        default_end="2026-09-18",
    )
    assert len(plan["jobs"]) == 3
    nse = next(job for job in plan["jobs"] if job["dataset"] == "cash_eod")
    assert "--retry-failed" in nse["command"]
    assert nse["mutex"] == "stock_market_db_writer"
    bse = next(job for job in plan["jobs"] if job["source"] == "BSE")
    assert "backfill_fundamentals.py" in bse["command"][1]
    screener = next(job for job in plan["jobs"] if job["source"] == "SCREENER")
    assert not screener["writes_database"]
    shell = render_shell(plan)
    assert "do not parallelize SQLite writers" in shell
    assert "MIN_FREE_GIB=12.0" in shell


def test_unsupported_retryable_family_is_not_executed() -> None:
    manifest = {
        "items": [
            {
                "source": "NSE",
                "dataset": "bulk_block_deals",
                "status": "retryable",
                "next_action": "download",
                "affected_count": 7,
            }
        ]
    }
    plan = build_plan(
        manifest,
        database_url="sqlite:///data/databases/stock_market.db",
    )
    assert plan["jobs"] == []
    assert plan["unsupported"][0]["dataset"] == "bulk_block_deals"
    assert plan["unsupported"][0]["affected_count"] == 7

from scripts.aaru_plan_downloads import build_plan, render_shell


def test_plan_bounds_prices_merges_nse_and_limits_bse_scrips() -> None:
    manifest = {
        "schema": "aaru.missing-manifest.v3",
        "items": [
            {
                "source": "NSE",
                "dataset": "cash_eod",
                "status": "retryable",
                "next_action": "download",
                "period_start": "2026-09-10",
                "period_end": "2026-09-12",
                "affected_count": 3,
            },
            {
                "source": "NSE",
                "dataset": "financial_results",
                "status": "retryable",
                "next_action": "download",
                "period_start": "2020-01-01",
                "period_end": "2026-09-18",
                "affected_count": 2,
            },
            {
                "source": "NSE",
                "dataset": "shareholding",
                "status": "retryable",
                "next_action": "download",
                "period_start": "2021-10-01",
                "period_end": "2026-09-18",
                "affected_count": 4,
            },
            {
                "source": "BSE",
                "dataset": "financial_results",
                "status": "retryable",
                "next_action": "download",
                "identity": "500325",
                "period_start": "2018-01-01",
                "period_end": "2026-09-18",
                "affected_count": 1,
            },
            {
                "source": "BSE",
                "dataset": "financial_results",
                "status": "retryable",
                "next_action": "download",
                "identity": "BSE:532540:Q1",
                "period_start": "2018-01-01",
                "period_end": "2026-09-18",
                "affected_count": 1,
            },
        ],
    }
    plan = build_plan(
        manifest,
        database_url="sqlite:///data/databases/stock_market.db",
        default_end="2026-09-18",
    )
    assert plan["schema"] == "aaru.download-plan.v2"
    assert len(plan["jobs"]) == 3

    prices = next(x for x in plan["jobs"] if x["dataset"] == "cash_eod")
    assert prices["command"][prices["command"].index("--start-date") + 1] == "2026-09-10"
    assert prices["command"][prices["command"].index("--end-date") + 1] == "2026-09-12"

    nse = next(x for x in plan["jobs"] if x["id"] == "nse-fundamentals")
    datasets = nse["command"][
        nse["command"].index("--datasets") + 1:
        nse["command"].index("--retry-failed")
    ]
    assert datasets == ["financial_results", "shareholding"]

    bse = next(x for x in plan["jobs"] if x["id"] == "bse-financial-results")
    codes = [
        bse["command"][i + 1]
        for i, value in enumerate(bse["command"])
        if value == "--bse-scrip-code"
    ]
    assert codes == ["500325", "532540"]
    assert not bse["broad_scope"]
    assert "Prefer scripts/aaru_execute_plan.py" in render_shell(plan)


def test_bse_shareholding_and_unmapped_families_are_unsupported() -> None:
    manifest = {
        "items": [
            {
                "source": "BSE",
                "dataset": "shareholding",
                "status": "retryable",
                "next_action": "download",
                "affected_count": 7,
            },
            {
                "source": "NSE",
                "dataset": "bulk_block_deals",
                "status": "retryable",
                "next_action": "download",
                "affected_count": 2,
            },
        ]
    }
    plan = build_plan(
        manifest,
        database_url="sqlite:///data/databases/stock_market.db",
    )
    assert plan["jobs"] == []
    unsupported = {(x["source"], x["dataset"]) for x in plan["unsupported"]}
    assert ("BSE", "shareholding") in unsupported
    assert ("NSE", "bulk_block_deals") in unsupported


def test_bse_without_codes_is_marked_broad_scope() -> None:
    manifest = {
        "items": [
            {
                "source": "BSE",
                "dataset": "financial_results",
                "status": "retryable",
                "next_action": "download",
                "identity": "unresolved",
                "affected_count": 1,
            }
        ]
    }
    plan = build_plan(
        manifest,
        database_url="sqlite:///data/databases/stock_market.db",
    )
    assert plan["jobs"][0]["broad_scope"] is True

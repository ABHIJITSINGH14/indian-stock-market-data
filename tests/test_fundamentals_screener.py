import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from fundamentals import (
    CompanySnapshot,
    Condition,
    Operator,
    SQLiteReadSchema,
    SQLiteSnapshotAdapter,
    ScreenRequest,
    Screener,
    Sort,
)
from fundamentals.screener import compute_values


class MemoryAdapter:
    def __init__(self, rows):
        self.rows = rows

    def snapshots(self, as_of, symbols=None):
        selected = set(symbols or ())
        return [
            item
            for item in self.rows
            if item.as_of <= as_of and (not selected or item.symbol in selected)
        ]


def snapshot(symbol, revenue, prior_revenue, pat=None):
    return CompanySnapshot(
        symbol=symbol,
        as_of=date(2025, 8, 1),
        period_end=date(2025, 6, 30),
        metrics={
            "revenue": revenue,
            "pat": pat,
            "operating_profit": Decimal("20") if revenue is not None else None,
            "pbt": Decimal("18") if revenue is not None else None,
            "finance_cost": Decimal("2") if revenue is not None else None,
            "assets": Decimal("200") if revenue is not None else None,
            "current_liabilities": Decimal("40") if revenue is not None else None,
            "current_assets": Decimal("80") if revenue is not None else None,
            "equity": Decimal("100") if revenue is not None else None,
            "debt": Decimal("25") if revenue is not None else None,
        },
        prior_metrics={"revenue": prior_revenue, "pat": Decimal("8")},
        market={
            "market_cap": Decimal("1000"),
            "price": Decimal("90"),
            "high_52w": Decimal("100"),
            "low_52w": Decimal("60"),
        },
    )


def test_derived_values_and_null_semantics():
    values = compute_values(snapshot("AAA", Decimal("120"), Decimal("100"), Decimal("10")))
    missing = compute_values(snapshot("NULL", None, Decimal("100"), None))

    assert values["sales_growth"] == Decimal("20")
    assert values["operating_margin"] == Decimal("16.66666666666666666666666667")
    assert values["roe"] == Decimal("10")
    assert values["roce"] == Decimal("12.5")
    assert values["debt_equity"] == Decimal("0.25")
    assert values["current_ratio"] == Decimal("2")
    assert values["high_52w_distance"] == Decimal("-10.0")
    assert missing["sales_growth"] is None
    assert missing["operating_margin"] is None


def test_safe_filters_sort_limit_and_missing_values():
    engine = Screener(
        MemoryAdapter(
            [
                snapshot("BBB", Decimal("150"), Decimal("100"), Decimal("12")),
                snapshot("AAA", Decimal("120"), Decimal("100"), Decimal("10")),
                snapshot("NULL", None, Decimal("100"), None),
            ]
        )
    )
    request = ScreenRequest(
        as_of=date(2025, 8, 1),
        conditions=(Condition("sales_growth", Operator.GT, Decimal("10")),),
        sort=(Sort("sales_growth", descending=True),),
        limit=1,
    )
    assert [item.symbol for item in engine.run(request)] == ["BBB"]

    null_request = ScreenRequest(
        as_of=date(2025, 8, 1),
        conditions=(Condition("revenue", Operator.IS_NULL),),
    )
    assert [item.symbol for item in engine.run(null_request)] == ["NULL"]

    descending = ScreenRequest(
        as_of=date(2025, 8, 1),
        sort=(Sort("sales_growth", descending=True),),
    )
    assert [item.symbol for item in engine.run(descending)] == ["BBB", "AAA", "NULL"]

    with pytest.raises(ValueError, match="unsupported screener field"):
        Condition("revenue; DROP TABLE facts", Operator.GT, Decimal("1"))
    with pytest.raises(TypeError, match="Decimal"):
        Condition("revenue", Operator.GT, 1)  # type: ignore[arg-type]


def test_sqlite_adapter_honors_as_of_revisions_and_schema_allowlist():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE fm (ticker TEXT, filed TEXT, period TEXT, name TEXT, amount TEXT);
        CREATE TABLE md (ticker TEXT, observed TEXT, name TEXT, amount TEXT);
        INSERT INTO fm VALUES
          ('AAA', '2025-05-01', '2025-03-31', 'revenue', '90'),
          ('AAA', '2025-05-20', '2025-03-31', 'revenue', '100'),
          ('AAA', '2025-08-01', '2025-06-30', 'revenue', '120'),
          ('AAA', '2025-08-10', '2025-06-30', 'revenue', '125');
        INSERT INTO md VALUES
          ('AAA', '2025-05-15', 'price', '50'),
          ('AAA', '2025-08-02', 'price', '60');
        """
    )
    schema = SQLiteReadSchema(
        metric_table="fm",
        market_table="md",
        metric_columns={
            "symbol": "ticker",
            "filing_date": "filed",
            "period_end": "period",
            "metric": "name",
            "value": "amount",
        },
        market_columns={
            "symbol": "ticker",
            "as_of": "observed",
            "metric": "name",
            "value": "amount",
        },
    )
    adapter = SQLiteSnapshotAdapter(connection, schema)

    early = tuple(adapter.snapshots(date(2025, 8, 5)))[0]
    late = tuple(adapter.snapshots(date(2025, 8, 20)))[0]
    assert early.metrics["revenue"] == Decimal("120")
    assert early.prior_metrics["revenue"] == Decimal("100")
    assert early.market["price"] == Decimal("60")
    assert late.metrics["revenue"] == Decimal("125")

    with pytest.raises(ValueError, match="allowlisted"):
        SQLiteReadSchema(
            metric_table="fm; DROP TABLE fm",
            market_table="md",
            metric_columns=schema.metric_columns,
            market_columns=schema.market_columns,
        )

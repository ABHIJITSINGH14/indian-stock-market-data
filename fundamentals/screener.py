"""Safe typed company screener with SQL-like NULL semantics."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple


class Operator(str, Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IS_NULL = "is_null"
    NOT_NULL = "not_null"


ALLOWED_FIELDS = frozenset(
    {
        "revenue",
        "pat",
        "operating_profit",
        "ebitda",
        "assets",
        "liabilities",
        "equity",
        "debt",
        "cash",
        "current_assets",
        "current_liabilities",
        "finance_cost",
        "depreciation_amortisation",
        "sales_growth",
        "profit_growth",
        "operating_margin",
        "net_margin",
        "roe",
        "roce",
        "debt_equity",
        "current_ratio",
        "market_cap",
        "pe",
        "pb",
        "dividend_yield",
        "high_52w_distance",
        "low_52w_distance",
        "price",
    }
)


@dataclass(frozen=True)
class CompanySnapshot:
    symbol: str
    as_of: date
    period_end: date
    metrics: Mapping[str, Optional[Decimal]]
    prior_metrics: Mapping[str, Optional[Decimal]] = field(default_factory=dict)
    market: Mapping[str, Optional[Decimal]] = field(default_factory=dict)


class SnapshotAdapter(Protocol):
    def snapshots(
        self, as_of: date, symbols: Optional[Sequence[str]] = None
    ) -> Iterable[CompanySnapshot]:
        """Return only information known on or before the requested date."""


@dataclass(frozen=True)
class Condition:
    field: str
    operator: Operator
    value: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if self.field not in ALLOWED_FIELDS:
            raise ValueError("unsupported screener field: {}".format(self.field))
        if self.operator in (Operator.IS_NULL, Operator.NOT_NULL):
            if self.value is not None:
                raise ValueError("{} does not accept a value".format(self.operator.value))
        elif self.value is None:
            raise ValueError("{} requires a numeric value".format(self.operator.value))
        elif not isinstance(self.value, Decimal):
            raise TypeError("condition value must be Decimal")


@dataclass(frozen=True)
class Sort:
    field: str
    descending: bool = False

    def __post_init__(self) -> None:
        if self.field not in ALLOWED_FIELDS and self.field not in ("symbol", "period_end"):
            raise ValueError("unsupported sort field: {}".format(self.field))


@dataclass(frozen=True)
class ScreenRequest:
    as_of: date
    conditions: Tuple[Condition, ...] = ()
    sort: Tuple[Sort, ...] = ()
    limit: int = 100
    symbols: Optional[Tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")


@dataclass(frozen=True)
class ScreenResult:
    symbol: str
    as_of: date
    period_end: date
    values: Mapping[str, Optional[Decimal]]


class Screener:
    def __init__(self, adapter: SnapshotAdapter) -> None:
        self.adapter = adapter

    def run(self, request: ScreenRequest) -> Tuple[ScreenResult, ...]:
        results: List[ScreenResult] = []
        for snapshot in self.adapter.snapshots(request.as_of, request.symbols):
            if snapshot.as_of > request.as_of:
                raise ValueError("adapter returned data after requested as_of")
            values = compute_values(snapshot)
            if all(_matches(values.get(item.field), item) for item in request.conditions):
                results.append(
                    ScreenResult(
                        snapshot.symbol,
                        request.as_of,
                        snapshot.period_end,
                        values,
                    )
                )

        for sort in reversed(request.sort):
            present = [
                item for item in results if _sort_value(item, sort.field) is not None
            ]
            missing = [
                item for item in results if _sort_value(item, sort.field) is None
            ]
            present.sort(
                key=lambda item, field=sort.field: _sort_value(item, field),
                reverse=sort.descending,
            )
            results[:] = present + missing
        return tuple(results[: request.limit])


def compute_values(snapshot: CompanySnapshot) -> Mapping[str, Optional[Decimal]]:
    values = {name: snapshot.metrics.get(name) for name in ALLOWED_FIELDS}
    values.update(
        {
            name: snapshot.market.get(name)
            for name in (
                "market_cap",
                "pe",
                "pb",
                "dividend_yield",
                "price",
            )
        }
    )
    revenue = snapshot.metrics.get("revenue")
    pat = snapshot.metrics.get("pat")
    operating_profit = snapshot.metrics.get("operating_profit")
    equity = snapshot.metrics.get("equity")
    assets = snapshot.metrics.get("assets")
    current_liabilities = snapshot.metrics.get("current_liabilities")
    debt = snapshot.metrics.get("debt")
    current_assets = snapshot.metrics.get("current_assets")
    finance_cost = snapshot.metrics.get("finance_cost")
    pbt = snapshot.metrics.get("pbt")
    price = snapshot.market.get("price")
    high_52w = snapshot.market.get("high_52w")
    low_52w = snapshot.market.get("low_52w")

    values["sales_growth"] = _growth(revenue, snapshot.prior_metrics.get("revenue"))
    values["profit_growth"] = _growth(pat, snapshot.prior_metrics.get("pat"))
    values["operating_margin"] = _percent(operating_profit, revenue)
    values["net_margin"] = _percent(pat, revenue)
    values["roe"] = _percent(pat, equity)
    capital_employed = _subtract(assets, current_liabilities)
    ebit = _sum_optional(pbt, finance_cost)
    values["roce"] = _percent(ebit, capital_employed)
    values["debt_equity"] = _divide(debt, equity)
    values["current_ratio"] = _divide(current_assets, current_liabilities)
    values["high_52w_distance"] = _distance(price, high_52w)
    values["low_52w_distance"] = _distance(price, low_52w)
    return values


def _matches(actual: Optional[Decimal], condition: Condition) -> bool:
    if condition.operator == Operator.IS_NULL:
        return actual is None
    if condition.operator == Operator.NOT_NULL:
        return actual is not None
    if actual is None:
        return False
    expected = condition.value
    assert expected is not None
    return {
        Operator.EQ: actual == expected,
        Operator.NE: actual != expected,
        Operator.GT: actual > expected,
        Operator.GTE: actual >= expected,
        Operator.LT: actual < expected,
        Operator.LTE: actual <= expected,
    }[condition.operator]


def _sort_value(result: ScreenResult, field: str) -> object:
    return (
        result.symbol
        if field == "symbol"
        else result.period_end
        if field == "period_end"
        else result.values.get(field)
    )


def _growth(current: Optional[Decimal], prior: Optional[Decimal]) -> Optional[Decimal]:
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / abs(prior) * Decimal(100)


def _percent(
    numerator: Optional[Decimal], denominator: Optional[Decimal]
) -> Optional[Decimal]:
    value = _divide(numerator, denominator)
    return value * Decimal(100) if value is not None else None


def _divide(
    numerator: Optional[Decimal], denominator: Optional[Decimal]
) -> Optional[Decimal]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _subtract(
    left: Optional[Decimal], right: Optional[Decimal]
) -> Optional[Decimal]:
    if left is None or right is None:
        return None
    return left - right


def _sum_optional(
    left: Optional[Decimal], right: Optional[Decimal]
) -> Optional[Decimal]:
    if left is None or right is None:
        return None
    return left + right


def _distance(price: Optional[Decimal], reference: Optional[Decimal]) -> Optional[Decimal]:
    if price is None or reference is None or reference == 0:
        return None
    return (price - reference) / reference * Decimal(100)

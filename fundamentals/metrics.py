"""Canonical metric mapping and context-aware quarterly selection."""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .domain import (
    ContextRecord,
    FactRecord,
    MetricRecord,
    MetricSource,
    ParsedFiling,
    PeriodKind,
    StatementScope,
)


def _normalized(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


DEFAULT_ALIASES: Mapping[str, Tuple[str, ...]] = {
    "revenue": (
        "RevenueFromOperations",
        "Revenue",
        "TotalRevenue",
        "InterestIncome",
        "RevenueFromOperationsNet",
    ),
    "other_income": ("OtherIncome",),
    "total_expenses": ("TotalExpenses", "Expenses"),
    "ebitda": (
        "EarningsBeforeInterestTaxesDepreciationAndAmortisation",
        "ProfitBeforeInterestTaxDepreciationAndAmortization",
        "OperatingProfitBeforeDepreciationInterestAndTax",
    ),
    "operating_profit": ("OperatingProfit", "ProfitFromOperationsBeforeOtherIncome"),
    "finance_cost": ("FinanceCosts", "FinanceCost"),
    "depreciation_amortisation": (
        "DepreciationDepletionAndAmortisationExpense",
        "DepreciationAndAmortisationExpense",
        "Depreciation",
    ),
    "pbt": ("ProfitBeforeTax", "ProfitLossBeforeTax"),
    "pat": (
        "ProfitLossForPeriod",
        "ProfitForThePeriod",
        "ProfitLoss",
        "NetProfitLossForThePeriod",
    ),
    "eps_basic": ("BasicEarningsLossPerShare", "BasicEarningsPerShare"),
    "assets": ("Assets", "TotalAssets"),
    "current_assets": ("CurrentAssets", "TotalCurrentAssets"),
    "liabilities": ("Liabilities", "TotalLiabilities"),
    "current_liabilities": ("CurrentLiabilities", "TotalCurrentLiabilities"),
    "equity": (
        "Equity",
        "EquityAttributableToOwnersOfParent",
        "TotalEquity",
        "ShareholdersFunds",
    ),
    "debt": (
        "Borrowings",
        "TotalBorrowings",
        "Debt",
    ),
    "cash": (
        "CashAndCashEquivalents",
        "CashAndBankBalances",
        "CashAndCashEquivalentsAtEndOfPeriod",
    ),
    "operating_cash_flow": (
        "CashFlowsFromUsedInOperatingActivities",
        "NetCashFlowsFromUsedInOperatingActivities",
    ),
    "investing_cash_flow": (
        "CashFlowsFromUsedInInvestingActivities",
        "NetCashFlowsFromUsedInInvestingActivities",
    ),
    "financing_cash_flow": (
        "CashFlowsFromUsedInFinancingActivities",
        "NetCashFlowsFromUsedInFinancingActivities",
    ),
    "capex": (
        "PurchaseOfPropertyPlantAndEquipment",
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "CapitalExpenditure",
    ),
}


@dataclass(frozen=True)
class ContextQuery:
    period_end: Optional[date] = None
    period: str = "quarter"
    scope: StatementScope = StatementScope.UNKNOWN
    current: bool = True

    def __post_init__(self) -> None:
        if self.period not in ("quarter", "year_to_date", "instant"):
            raise ValueError("period must be quarter, year_to_date, or instant")


class MetricMapper:
    """Maps facts by local-name aliases while retaining raw facts unchanged."""

    def __init__(
        self, aliases: Optional[Mapping[str, Sequence[str]]] = None
    ) -> None:
        merged = {name: tuple(values) for name, values in DEFAULT_ALIASES.items()}
        if aliases:
            for name, values in aliases.items():
                merged[name] = tuple(values)
        self.aliases = merged
        self._concept_to_metric = {
            _normalized(alias): metric
            for metric, concepts in merged.items()
            for alias in concepts
        }
        self._alias_rank = {
            (metric, _normalized(alias)): index
            for metric, concepts in merged.items()
            for index, alias in enumerate(concepts)
        }

    def map_filing(
        self, filing: ParsedFiling, query: ContextQuery
    ) -> Tuple[MetricRecord, ...]:
        target_end = query.period_end or _target_accounting_date(filing.facts, query)
        if target_end is None:
            return ()
        candidates: Dict[str, List[FactRecord]] = {}
        for fact in filing.facts:
            if fact.numeric_value is None:
                continue
            metric = self._concept_to_metric.get(_normalized(fact.local_name))
            if (
                metric
                and _scope_matches(fact, query)
                and _context_matches(fact.context, query, target_end)
            ):
                candidates.setdefault(metric, []).append(fact)

        mapped: Dict[str, MetricRecord] = {}
        for metric, facts in candidates.items():
            selected = min(
                facts,
                key=lambda item: (
                    _context_rank(item.context, query, target_end),
                    self._alias_rank[(metric, _normalized(item.local_name))],
                    _namespace_version_rank(item.namespace),
                    item.namespace,
                ),
            )
            mapped[metric] = _reported(metric, selected)
        _derive_metrics(mapped)
        return tuple(mapped[name] for name in sorted(mapped))


def infer_scope(context: ContextRecord) -> StatementScope:
    dimension_text = " ".join(
        (dimension.dimension_name + " " + dimension.member_name).lower()
        for dimension in context.dimensions
    )
    if "consolidated" in dimension_text or "group" in dimension_text:
        return StatementScope.CONSOLIDATED
    if "standalone" in dimension_text or "separate" in dimension_text:
        return StatementScope.STANDALONE
    return StatementScope.UNKNOWN


def _target_accounting_date(
    facts: Iterable[FactRecord], query: ContextQuery
) -> Optional[date]:
    dates = sorted(
        {
            fact.context.accounting_date
            for fact in facts
            if fact.context.accounting_date
            and _period_kind_matches(fact.context, query.period)
        },
        reverse=True,
    )
    index = 0 if query.current else 1
    return dates[index] if len(dates) > index else None


def _period_kind_matches(context: ContextRecord, period: str) -> bool:
    if period == "instant":
        return context.period_kind == PeriodKind.INSTANT
    if context.period_kind != PeriodKind.DURATION:
        return False
    days = _duration_days(context)
    if days is None:
        return False
    return 75 <= days <= 105 if period == "quarter" else 150 <= days <= 380


def _duration_days(context: ContextRecord) -> Optional[int]:
    if context.period_start is None or context.period_end is None:
        return None
    return (context.period_end - context.period_start).days + 1


def _namespace_version_rank(namespace: str) -> Tuple[int, ...]:
    versions = tuple(int(value) for value in re.findall(r"\d+", namespace))
    return tuple(-value for value in versions) if versions else (0,)


def _context_matches(
    context: ContextRecord, query: ContextQuery, target_end: Optional[date]
) -> bool:
    if target_end and context.accounting_date != target_end:
        return False
    if query.period == "instant":
        return context.period_kind == PeriodKind.INSTANT
    if context.period_kind != PeriodKind.DURATION:
        return False
    days = _duration_days(context)
    if days is None:
        return False
    if query.period == "quarter":
        return 75 <= days <= 105
    return 150 <= days <= 380


def _scope_matches(fact: FactRecord, query: ContextQuery) -> bool:
    if query.scope == StatementScope.UNKNOWN:
        return True
    scope = infer_scope(fact.context)
    if scope != StatementScope.UNKNOWN:
        return scope == query.scope
    if fact.filing.consolidated is None:
        return True
    filing_scope = (
        StatementScope.CONSOLIDATED
        if fact.filing.consolidated
        else StatementScope.STANDALONE
    )
    return filing_scope == query.scope


def _context_rank(
    context: ContextRecord, query: ContextQuery, target_end: Optional[date]
) -> Tuple[int, int, str]:
    inferred = infer_scope(context)
    scope_rank = 0 if inferred == query.scope else 1 if inferred == StatementScope.UNKNOWN else 2
    days = _duration_days(context) or 0
    expected = 90 if query.period == "quarter" else 270
    duration_rank = abs(days - expected) if query.period != "instant" else 0
    return scope_rank, duration_rank, context.context_id


def _reported(name: str, fact: FactRecord) -> MetricRecord:
    assert fact.numeric_value is not None
    return MetricRecord(
        filing=fact.filing,
        context=fact.context,
        name=name,
        value=fact.numeric_value,
        unit_ref=fact.unit_ref,
        source=MetricSource.REPORTED,
        concept_qnames=(fact.qname,),
    )


def _derive_metrics(mapped: Dict[str, MetricRecord]) -> None:
    if "ebitda" not in mapped:
        _derive_sum(
            mapped,
            "ebitda",
            ("pbt", "finance_cost", "depreciation_amortisation"),
            "pbt + finance_cost + depreciation_amortisation",
        )
    if "operating_profit" not in mapped:
        if "ebitda" in mapped and "depreciation_amortisation" in mapped:
            _derive(
                mapped,
                "operating_profit",
                mapped["ebitda"].value
                - mapped["depreciation_amortisation"].value,
                ("ebitda", "depreciation_amortisation"),
                "ebitda - depreciation_amortisation",
            )
    if "liabilities" not in mapped and "assets" in mapped and "equity" in mapped:
        _derive(
            mapped,
            "liabilities",
            mapped["assets"].value - mapped["equity"].value,
            ("assets", "equity"),
            "assets - equity",
        )


def _derive_sum(
    mapped: Dict[str, MetricRecord],
    output: str,
    inputs: Sequence[str],
    expression: str,
) -> None:
    if all(name in mapped for name in inputs):
        _derive(
            mapped,
            output,
            sum((mapped[name].value for name in inputs), Decimal(0)),
            inputs,
            expression,
        )


def _derive(
    mapped: Dict[str, MetricRecord],
    output: str,
    value: Decimal,
    inputs: Sequence[str],
    expression: str,
) -> None:
    first = mapped[inputs[0]]
    qnames: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for name in inputs:
        for qname in mapped[name].concept_qnames:
            if qname not in seen:
                seen.add(qname)
                qnames.append(qname)
    mapped[output] = MetricRecord(
        filing=first.filing,
        context=first.context,
        name=output,
        value=value,
        unit_ref=first.unit_ref,
        source=MetricSource.DERIVED,
        concept_qnames=tuple(qnames),
        derivation=expression,
    )

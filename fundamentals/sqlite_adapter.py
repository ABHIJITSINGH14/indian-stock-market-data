"""Configurable SQLite reader for the screener; this module owns no schema."""

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .screener import CompanySnapshot

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SQLiteReadSchema:
    metric_table: str
    market_table: str
    metric_columns: Mapping[str, str]
    market_columns: Mapping[str, str]

    def __post_init__(self) -> None:
        identifiers = [self.metric_table, self.market_table]
        identifiers.extend(self.metric_columns.values())
        identifiers.extend(self.market_columns.values())
        if not all(_IDENTIFIER.fullmatch(item) for item in identifiers):
            raise ValueError("SQLite table and column identifiers must be allowlisted names")
        required_metric = {
            "symbol",
            "filing_date",
            "period_end",
            "metric",
            "value",
        }
        required_market = {"symbol", "as_of", "metric", "value"}
        if not required_metric.issubset(self.metric_columns):
            raise ValueError("metric_columns is missing required logical columns")
        if not required_market.issubset(self.market_columns):
            raise ValueError("market_columns is missing required logical columns")


class SQLiteSnapshotAdapter:
    """Read long-form metric rows through an explicitly supplied schema mapping."""

    def __init__(self, connection: sqlite3.Connection, schema: SQLiteReadSchema) -> None:
        self.connection = connection
        self.schema = schema

    def snapshots(
        self, as_of: date, symbols: Optional[Sequence[str]] = None
    ) -> Iterable[CompanySnapshot]:
        financial_rows = self._fetch_rows(
            self.schema.metric_table,
            self.schema.metric_columns,
            "filing_date",
            as_of,
            symbols,
        )
        market_rows = self._fetch_rows(
            self.schema.market_table,
            self.schema.market_columns,
            "as_of",
            as_of,
            symbols,
        )
        return _assemble_snapshots(financial_rows, market_rows, as_of)

    def _fetch_rows(
        self,
        table: str,
        columns: Mapping[str, str],
        date_field: str,
        as_of: date,
        symbols: Optional[Sequence[str]],
    ) -> List[sqlite3.Row]:
        logical_fields = list(columns)
        select = ", ".join(
            "{} AS {}".format(columns[field], field) for field in logical_fields
        )
        sql = "SELECT {} FROM {} WHERE {} <= ?".format(
            select, table, columns[date_field]
        )
        params: List[str] = [as_of.isoformat()]
        if symbols:
            sql += " AND {} IN ({})".format(
                columns["symbol"], ", ".join("?" for _ in symbols)
            )
            params.extend(symbols)
        sql += " ORDER BY {}, {}".format(columns["symbol"], columns[date_field])
        cursor = self.connection.execute(sql, params)
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]  # type: ignore[return-value]


def _assemble_snapshots(
    financial_rows: Sequence[Mapping[str, object]],
    market_rows: Sequence[Mapping[str, object]],
    as_of: date,
) -> Tuple[CompanySnapshot, ...]:
    by_symbol_period: DefaultDict[
        str, DefaultDict[date, Dict[str, Tuple[date, Decimal]]]
    ] = defaultdict(lambda: defaultdict(dict))
    for row in financial_rows:
        symbol = str(row["symbol"])
        filing_date = date.fromisoformat(str(row["filing_date"])[:10])
        period_end = date.fromisoformat(str(row["period_end"])[:10])
        metric = str(row["metric"])
        value = Decimal(str(row["value"]))
        existing = by_symbol_period[symbol][period_end].get(metric)
        if existing is None or filing_date >= existing[0]:
            by_symbol_period[symbol][period_end][metric] = (filing_date, value)

    market_by_symbol: DefaultDict[str, Dict[str, Tuple[date, Decimal]]] = defaultdict(dict)
    for row in market_rows:
        symbol = str(row["symbol"])
        observed = date.fromisoformat(str(row["as_of"])[:10])
        metric = str(row["metric"])
        value = Decimal(str(row["value"]))
        existing = market_by_symbol[symbol].get(metric)
        if existing is None or observed >= existing[0]:
            market_by_symbol[symbol][metric] = (observed, value)

    snapshots = []
    for symbol, periods in by_symbol_period.items():
        ordered_periods = sorted(periods, reverse=True)
        if not ordered_periods:
            continue
        current_period = ordered_periods[0]
        prior_period = ordered_periods[1] if len(ordered_periods) > 1 else None
        metrics = {name: item[1] for name, item in periods[current_period].items()}
        prior = (
            {name: item[1] for name, item in periods[prior_period].items()}
            if prior_period
            else {}
        )
        market = {name: item[1] for name, item in market_by_symbol[symbol].items()}
        snapshots.append(
            CompanySnapshot(symbol, as_of, current_period, metrics, prior, market)
        )
    return tuple(sorted(snapshots, key=lambda item: item.symbol))


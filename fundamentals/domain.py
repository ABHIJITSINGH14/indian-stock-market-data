"""Storage-neutral domain records for quarterly financial filings."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping, Optional, Tuple


class StatementScope(str, Enum):
    STANDALONE = "standalone"
    CONSOLIDATED = "consolidated"
    UNKNOWN = "unknown"


class PeriodKind(str, Enum):
    DURATION = "duration"
    INSTANT = "instant"
    FOREVER = "forever"


class MetricSource(str, Enum):
    REPORTED = "reported"
    DERIVED = "derived"


@dataclass(frozen=True)
class FilingIdentity:
    symbol: str
    filing_date: date
    source_url: str
    company_name: Optional[str] = None
    isin: Optional[str] = None
    industry: Optional[str] = None
    financial_year: Optional[str] = None
    relating_to: Optional[str] = None
    sequence_number: Optional[str] = None
    exchange_timestamp: Optional[datetime] = None
    audited: Optional[bool] = None
    consolidated: Optional[bool] = None
    cumulative: Optional[bool] = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def key(self) -> Tuple[str, date, str, Optional[str]]:
        return self.symbol, self.filing_date, self.source_url, self.sequence_number


@dataclass(frozen=True)
class Dimension:
    dimension_namespace: str
    dimension_name: str
    member_namespace: str
    member_name: str
    typed_value: Optional[str] = None


@dataclass(frozen=True)
class ContextRecord:
    context_id: str
    entity_identifier: str
    entity_scheme: Optional[str]
    period_kind: PeriodKind
    period_start: Optional[date]
    period_end: Optional[date]
    instant: Optional[date]
    dimensions: Tuple[Dimension, ...] = ()

    @property
    def accounting_date(self) -> Optional[date]:
        return self.instant or self.period_end


@dataclass(frozen=True)
class UnitRecord:
    unit_id: str
    measures: Tuple[str, ...]
    numerator_measures: Tuple[str, ...] = ()
    denominator_measures: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FactRecord:
    filing: FilingIdentity
    context: ContextRecord
    namespace: str
    local_name: str
    value: str
    numeric_value: Optional[Decimal]
    unit_ref: Optional[str]
    decimals: Optional[str]
    precision: Optional[str]
    nil: bool = False

    @property
    def qname(self) -> Tuple[str, str]:
        return self.namespace, self.local_name


@dataclass(frozen=True)
class MetricRecord:
    filing: FilingIdentity
    context: ContextRecord
    name: str
    value: Decimal
    unit_ref: Optional[str]
    source: MetricSource
    concept_qnames: Tuple[Tuple[str, str], ...]
    derivation: Optional[str] = None


@dataclass(frozen=True)
class ParsedFiling:
    identity: FilingIdentity
    contexts: Tuple[ContextRecord, ...]
    units: Tuple[UnitRecord, ...]
    facts: Tuple[FactRecord, ...]
    unknown_context_fact_count: int = 0
    unknown_context_ids: Tuple[str, ...] = ()

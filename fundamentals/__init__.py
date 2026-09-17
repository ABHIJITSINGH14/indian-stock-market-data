"""Quarterly XBRL parsing and safe company screening."""

from .domain import (
    ContextRecord,
    FactRecord,
    FilingIdentity,
    MetricRecord,
    MetricSource,
    ParsedFiling,
    PeriodKind,
    StatementScope,
    UnitRecord,
)
from .metrics import ContextQuery, MetricMapper
from .screener import (
    CompanySnapshot,
    Condition,
    Operator,
    ScreenRequest,
    ScreenResult,
    Screener,
    SnapshotAdapter,
    Sort,
)
from .sqlite_adapter import SQLiteReadSchema, SQLiteSnapshotAdapter
from .xbrl import XBRLParseError, parse_xbrl

__all__ = [
    "CompanySnapshot",
    "Condition",
    "ContextQuery",
    "ContextRecord",
    "FactRecord",
    "FilingIdentity",
    "MetricMapper",
    "MetricRecord",
    "MetricSource",
    "Operator",
    "ParsedFiling",
    "PeriodKind",
    "SQLiteReadSchema",
    "SQLiteSnapshotAdapter",
    "ScreenRequest",
    "ScreenResult",
    "Screener",
    "SnapshotAdapter",
    "Sort",
    "StatementScope",
    "UnitRecord",
    "XBRLParseError",
    "parse_xbrl",
]

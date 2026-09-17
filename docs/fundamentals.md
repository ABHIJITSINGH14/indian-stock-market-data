# Quarterly XBRL fundamentals

`fundamentals` is a storage-neutral Python 3.9 package. It parses linked NSE
financial-result XBRL, maps raw facts to canonical metrics, and screens
point-in-time company snapshots. It does not fetch NSE data, create tables, or
participate in the central ingestion workflow.

## Parser API

Create a `FilingIdentity` from the NSE index record, download the linked `xbrl`
URL in the ingestion layer, then parse its bytes:

```python
from datetime import date
from fundamentals import FilingIdentity, parse_xbrl

filing = parse_xbrl(
    xml_bytes,
    FilingIdentity(
        symbol="RELIANCE",
        filing_date=date(2025, 7, 18),
        source_url=xbrl_url,
        sequence_number="12345",
        consolidated=True,
    ),
)
```

`ParsedFiling` contains immutable normalized contexts, units, and facts. Every
fact retains the filing identity, context/entity/period/dimensions, namespace
and local name, raw value, parsed `Decimal` where numeric, unit, decimals,
precision, and nil state. Parsing disables entity expansion, DTD loading, and
network access and rejects entity/DOCTYPE declarations.

Map a selected accounting context separately:

```python
from fundamentals import ContextQuery, MetricMapper, StatementScope

metrics = MetricMapper().map_filing(
    filing,
    ContextQuery(
        period_end=date(2025, 6, 30),
        period="quarter",
        scope=StatementScope.CONSOLIDATED,
    ),
)
```

`period` is `quarter`, `year_to_date`, or `instant`. `current=False` selects
the preceding matching accounting date when `period_end` is omitted. Filing
dates remain metadata and are never confused with accounting dates. Raw facts
remain available even when no alias maps them. Pass additional aliases to
`MetricMapper({"revenue": ("BankSpecificRevenueConcept",)})` for taxonomy,
bank, or insurer extensions.

## Canonical metrics

The built-in aliases cover revenue, other income, expenses, EBITDA, operating
profit, finance cost, depreciation/amortisation, PBT, PAT/profit-loss, basic
EPS, assets/current assets, liabilities/current liabilities, equity, debt,
cash, operating/investing/financing cash flow, and capex. Mapping uses QName
local names so taxonomy namespace/version changes do not break aliases.

Each `MetricRecord` is either `reported` with its source QName or `derived`
with all input QNames and an explicit formula. Derivations occur only when all
inputs exist: EBITDA = PBT + finance cost + depreciation/amortisation;
operating profit = EBITDA - depreciation/amortisation;
liabilities = assets - equity. Missing facts stay missing.

## Screener API

The screener accepts typed, allowlisted fields and operators; it never evaluates
Python or accepts raw SQL:

```python
from decimal import Decimal
from fundamentals import Condition, Operator, ScreenRequest, Screener, Sort

rows = Screener(adapter).run(
    ScreenRequest(
        as_of=date(2025, 8, 1),
        conditions=(
            Condition("sales_growth", Operator.GT, Decimal("10")),
            Condition("debt_equity", Operator.LTE, Decimal("1")),
        ),
        sort=(Sort("roe", descending=True),),
        limit=50,
    )
)
```

Supported derived fields are sales/profit growth, operating/net margin, ROE,
ROCE, debt/equity, current ratio, and distance from 52-week high/low. Market
fields are market cap, P/E, P/B, dividend yield, and price. Division by zero
and missing inputs produce `None`; ordinary comparisons with `None` are false.
Use `IS_NULL`/`NOT_NULL` explicitly.

## Persistence integration

Implement `SnapshotAdapter.snapshots(as_of, symbols)` to return
`CompanySnapshot` objects containing the latest information known by `as_of`,
including current/prior period metric maps and market observations. This is the
stable integration boundary for the future SQLite foundation.

`SQLiteSnapshotAdapter` is an optional long-form reader. Supply
`SQLiteReadSchema` with table and column names; all identifiers are validated
against a strict identifier allowlist and all values are bound parameters. It
expects logical financial columns `symbol`, `filing_date`, `period_end`,
`metric`, `value`, and market columns `symbol`, `as_of`, `metric`, `value`.
It selects the latest revision and observation available by the requested
date. The adapter creates and mutates no schema.

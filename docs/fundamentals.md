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

## Historical all-company backfill

The installed `stock-fundamentals-backfill` command (or
`python scripts/backfill_fundamentals.py`) uses NSE's bulk date-window indexes:

- `corporates-financial-results` with `period=Quarterly`
- `corporate-share-holdings-master`

One request covers all equity symbols in a window. Linked XBRL downloads use a
global minimum interval plus bounded worker concurrency; all SQLite parsing and
writes remain on the coordinator thread. Every index row is stored before its
document is attempted. Filings are keyed by exchange, dataset, official filing
identity/revision, and document SHA, so a corrected document is preserved
instead of replacing the original.

```bash
stock-fundamentals-backfill backfill \
  --database-url sqlite:///data/databases/stock_market.db \
  --start-date 2007-02-01 \
  --end-date 2026-09-18 \
  --datasets financial_results shareholding \
  --window-days 30 \
  --workers 3 \
  --request-delay 0.75
```

Completed windows are skipped by default. A partial window is safe to rerun:
successful documents come from the SHA-addressed local cache and only missing
or failed documents are downloaded.

```bash
# Retry failed/partial/interrupted and previously unseen windows.
stock-fundamentals-backfill backfill \
  --database-url sqlite:///data/databases/stock_market.db \
  --start-date 2007-02-01 --end-date 2026-09-18 \
  --retry-failed

# Revisit every window and force a document refresh.
stock-fundamentals-backfill backfill \
  --database-url sqlite:///data/databases/stock_market.db \
  --start-date 2007-02-01 --end-date 2026-09-18 \
  --no-resume --refresh-documents
```

`403` and `429` responses use bounded exponential backoff and open a circuit
breaker after repeated blocks. `SIGINT`/Ctrl-C marks the active window
`interrupted` and exits with status 130. Other failed documents are recorded
and do not prevent the remaining filings or windows from completing.

## Coverage report

```bash
stock-fundamentals-backfill report \
  --database-url sqlite:///data/databases/stock_market.db
stock-fundamentals-backfill report \
  --database-url sqlite:///data/databases/stock_market.db --json
```

The report includes structured financial and index symbol counts, accounting
periods, filings, lossless raw facts, canonical metric completeness by metric,
ownership symbols/quarters and promoter/FII/DII/public completeness, stored,
missing, and broken documents, checkpoint statuses, and the latest filings.

## Official-source limitations

Live bounded probes on 18 September 2026 established these public limits:

- NSE financial-result metadata is dense from **2007-02-01**. All 2005-2006
  period probes were empty.
- Valid NSE financial XBRL begins **2018-05-21**. Older index rows are still
  retained, but their `xbrl` field commonly ends in the invalid `/-`
  placeholder.
- NSE shareholding has sparse migrated records from **2016-01-13** and isolated
  XBRL from **2018-04-13**, but broad all-company public bulk coverage begins
  **2021-10-01**. Earlier ownership history must be labelled incomplete.
- BSE's bulk result-announcement/PDF archive begins **2011-04-20**. PDFs are
  retained only as index provenance and are never converted to metrics.
- BSE's current `Corp_FinanceResult_ng_new/w` index exposes genuine XBRL through
  `/XBRLFILES/`. Four representative issuer probes first exposed XBRL between
  **2018-07-23** and **2018-10-11**. Historical discovery is per scrip code
  (`FlagDur=7`), not an arbitrary-date bulk endpoint, so the runner checkpoints
  each BSE scrip separately.

NSE financial metadata before XBRL remains useful index provenance but cannot
produce structured facts. Empty older windows do not prove that no filing
existed outside the currently public archive. The backfill stores only official
index values and linked XML/XBRL facts; it never substitutes scraped aggregators
or fabricates missing metrics.

For maximum official coverage, the full command includes both exchanges:

```bash
stock-fundamentals-backfill backfill \
  --database-url sqlite:///data/databases/stock_market.db \
  --exchange all \
  --start-date 2007-02-01 --end-date 2026-09-18 \
  --datasets financial_results shareholding \
  --window-days 30 --workers 3 --request-delay 0.75
```

NSE uses bulk all-company windows. BSE financial history necessarily uses one
official historical-index request per BSE scrip; `--bse-scrip-code` can bound a
smoke or repair run.

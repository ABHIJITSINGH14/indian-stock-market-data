# Usage

All commands work from the repository root using either
`python scripts/run_all.py` or `python -m scripts.run_all`.

## Commands

```bash
# Create or migrate the default SQLite database without network access
python scripts/run_all.py init-db

# Refresh both complete official equity security masters
python scripts/run_all.py masters

# Refresh only one master
python scripts/run_all.py masters --exchange nse
python scripts/run_all.py masters --exchange bse

# Ingest a specific historical range from both exchanges
python scripts/run_all.py prices \
  --start-date 2024-01-01 \
  --end-date 2024-12-31

# Refresh masters, then prices
python scripts/run_all.py all \
  --start-date 2024-01-01 \
  --end-date 2024-01-31

# Retry all recorded failed dates up to the end date
python scripts/run_all.py prices --retry-failed --end-date 2024-12-31
```

Without `--start-date`, price ingestion starts one day after that source's
latest successful checkpoint. On a new database it requests only
`--end-date` (today by default), making the default command a practical daily
refresh rather than an accidental multi-year download.

Useful controls:

```text
--database-url URL       SQLite database URL
--exchange nse|bse|all   Source selection
--request-delay SECONDS  Delay between official requests (default 0.5)
--timeout SECONDS        Per-request timeout (default 30)
--retries COUNT          Retry count for transient HTTP failures (default 3)
--verbose                Debug logging
```

## Incremental and failure behavior

Each source/date is an independent checkpoint. A successful date is skipped on
rerun; its rows are also protected by the `daily_prices` primary key and are
updated rather than duplicated if explicitly processed again. A failed date is
stored with its error, processing continues, and the process exits with status
1. No Yahoo or other unofficial fallback is used.

Date ranges include weekdays. Market holidays have no archive and are therefore
reported as source/date failures. This is intentional: the system does not
guess whether a missing official archive is a holiday, a transient outage, or
an upstream change.

## SQLite schema and querying

Default location: `data/databases/stock_market.db`.

```sql
SELECT s.canonical_id, s.isin, s.name,
       x.exchange, x.exchange_symbol, x.series, x.scrip_code
FROM securities AS s
JOIN exchange_symbols AS x ON x.security_id = s.id;

SELECT s.name, p.exchange, p.trading_date, p.close, p.volume
FROM daily_prices AS p
JOIN securities AS s ON s.id = p.security_id
WHERE s.isin = 'INE002A01018'
ORDER BY p.trading_date;
```

ISIN is the cross-exchange identity when valid and available. Missing ISINs use
a deterministic exchange-scoped hash of symbol plus series/scrip code, avoiding
unsafe name-based merging. Raw symbols and response rows remain available for
audit.

## Storage and runtime expectations

A single recent bhavcopy typically contains thousands of rows. A daily refresh
takes seconds to minutes depending on exchange throttling. Multi-year ranges can
take hours because requests are deliberately rate-limited. Allow roughly
1-5 GB for broad multi-year NSE+BSE history; actual size varies with coverage
and SQLite page growth.

The legacy fundamentals, deals, corporate-action, and analysis scripts retain
their CSV outputs under `data/raw/`. They are not part of the official
master/bhavcopy transaction and are not run by the new ingestion CLI.

## Disclosures and quarterly XBRL fundamentals

The disclosure collector writes to the same database as the bhavcopy pipeline:

```bash
# All NSE filing indexes and linked documents for a filing-date range
python scripts/collect_disclosures.py \
  --start-date 2026-07-01 --end-date 2026-09-18

# One company, useful for a first XBRL run
python scripts/collect_disclosures.py \
  --symbol RELIANCE --start-date 2025-01-01 --end-date 2026-09-18

# Stable BSE per-scrip enrichment
python scripts/collect_disclosures.py \
  --exchange bse --bse-scrip-code 500325 \
  --datasets corporate_actions board_meetings pit sast financial_results

# Index-only run without downloading XBRL/attachments
python scripts/collect_disclosures.py \
  --skip-documents --start-date 2026-09-01 --end-date 2026-09-18
```

NSE requests are split into bounded date windows. The collector stores raw
index JSON, keeps original and revised filings, archives linked documents by
SHA-256, and parses XBRL without DTD/entity/network resolution. Filing dates
remain distinct from accounting periods. FII/DII ownership is derived from
taxonomy members; official public ownership is stored separately rather than
mislabelled as retail.

The official BSE `FinancialResult/w` endpoint currently exposes an HTML
document index, not consistent machine-readable XBRL facts. Its raw index is
retained for audit while structured financial facts come from official NSE
XBRL. Known-broken BSE shareholding routes are intentionally not treated as a
successful data source.

## Fundamental screener

The screener accepts allowlisted typed filters; it never evaluates raw Python or
injects user expressions into SQL:

```bash
python scripts/screen.py \
  --as-of 2026-09-18 \
  --where sales_growth:gte:10 \
  --where roe:gte:15 \
  --where debt_equity:lte:1 \
  --sort roe:desc \
  --limit 50
```

The integrated collector supplies reported fundamentals, growth, margins, ROE,
ROCE, debt/equity, current ratio, price, and 52-week high/low distance. The
screener API can also consume market cap, P/E, P/B, and dividend yield when a
caller supplies those optional observations through another storage adapter;
the official collectors do not fabricate them. Missing inputs remain unknown
(`NULL`) instead of being converted to zero. See `docs/fundamentals.md` for
metric definitions and context-selection semantics.

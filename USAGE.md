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

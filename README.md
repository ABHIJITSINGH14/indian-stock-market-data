# Indian Stock Market Data

A local-first NSE/BSE equity data platform backed by SQLite. The ingestion core
downloads official exchange security masters and bhavcopies for the full
available equity universe; it does not substitute Yahoo data when an official
feed fails.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

python scripts/run_all.py init-db
python scripts/run_all.py masters
python scripts/run_all.py prices --start-date 2024-01-01 --end-date 2024-01-31

# Maximum official history; safely resume by running the same command again
python scripts/backfill.py backfill --exchange all --workers 4 --request-delay 1.0

# Human-readable or JSON coverage/gap report
python scripts/backfill.py coverage --exchange all
python scripts/backfill.py coverage --exchange all --json

# Official filings, XBRL fundamentals, ownership and regulatory disclosures
python scripts/collect_disclosures.py \
  --exchange all --start-date 2026-07-01 --end-date 2026-09-18

# Resumable all-company NSE financial and ownership history
python scripts/backfill_fundamentals.py backfill \
  --start-date 2007-02-01 --end-date 2026-09-18 \
  --datasets financial_results shareholding
python scripts/backfill_fundamentals.py report

# Safe local fundamental screen
python scripts/screen.py --where roe:gte:15 --where debt_equity:lt:1 \
  --sort roe:desc --limit 25
```

Module execution is equivalent:

```bash
python -m scripts.run_all all --start-date 2024-01-01 --end-date 2024-01-31
```

The default database is `data/databases/stock_market.db`. Repeating a command
is safe: security mappings and daily prices are upserted transactionally, and
successful price dates are skipped. Failed exchange/date downloads are recorded
and make the command exit non-zero while other dates and sources continue.

The dedicated historical backfill defaults to NSE `1994-11-03` and BSE
`2006-03-01`, the first verified reachable files in the exchanges' public
daily archives. There are no official annual bulk archives. Public BSE files
before March 2006 are not available; a true 40-year BSE history requires a paid
BSE data product, and this project does not substitute unofficial sources.

## Data model

- `securities`: canonical company/security identity, preferring ISIN.
- `exchange_symbols`: raw and normalized NSE/BSE aliases, series, and BSE scrip
  codes linked to one canonical security.
- `daily_prices`: exchange/date/series OHLCV bhavcopy observations. Parsed typed
  fields and source provenance are retained; duplicate per-row source JSON is
  omitted so multi-decade history remains practical in local SQLite.
- `ingestion_runs`, `ingestion_checkpoints`, `ingestion_errors`: truthful run
  status, incremental progress, and source failures.
- `filings`, `raw_documents`: revision-aware filing indexes and content-hashed
  official XML/XBRL/attachment archives.
- `shareholding_patterns`: promoter, FII/FPI, DII, public, and residual
  non-institutional public ownership by symbol and quarter.
- `financial_facts`, `financial_metrics`: taxonomy-independent raw XBRL facts
  and canonical quarterly metrics with filing/as-of provenance.
- `financial_fact_instances`: lossless XBRL facts in source order, including
  entity, context, period kind, dimensions, precision, nil state, and units.
- `filing_index_checkpoints`, `filing_document_status`: resumable bulk-window
  and linked-document state, including explicit partial/failure diagnostics.
- `corporate_actions`, `board_meetings`, `pit_disclosures`,
  `sast_disclosures`: normalized exchange disclosures linked to securities.
- `institutional_activity`: market-wide daily FII/FPI and DII cash activity.
- `market_metrics`: price and rolling 52-week observations used by the screener.
- `schema_migrations`: locally applied schema version.

Legacy ancillary scripts continue to produce ignored CSV files under
`data/raw/`. SQLite is the durable source of truth for equity masters and daily
prices. See [INSTALLATION.md](INSTALLATION.md) and [USAGE.md](USAGE.md).

## Source notes

NSE uses its official equity master CSV and legacy/UDiFF bhavcopy ZIP archives.
BSE uses its official active-scrip API and legacy/UDiFF equity bhavcopy ZIP
archives. Exchanges may omit archives on weekends and market holidays; narrow
explicit date ranges avoid recording expected non-trading days as failures.

NSE bulk corporate-filing APIs and their linked XBRL instances are the primary
structured source for shareholding and quarterly financial facts. Stable BSE
per-scrip APIs enrich corporate actions, meetings, PIT and SAST disclosures.
BSE's current shareholding API is not automated because it redirects to an
exchange error page; the collector does not silently replace it with
unofficial data. Review exchange terms before redistributing archived filings.

The historical backfill calls each NSE date-window index once for the complete
equity universe; it never loops over symbols when the bulk endpoint is
available. Linked XBRL downloads are content-addressed, rate-limited, bounded
in concurrency, and written to SQLite serially. BSE official financial XBRL is
also supported from its per-scrip historical index; BSE PDFs are not parsed or
converted into invented metrics. See
[docs/fundamentals.md](docs/fundamentals.md) for resume, retry, coverage, and
source-history details.

## License

MIT License. Data remains subject to the exchanges' terms of use.

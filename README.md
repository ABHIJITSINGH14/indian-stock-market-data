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
```

Module execution is equivalent:

```bash
python -m scripts.run_all all --start-date 2024-01-01 --end-date 2024-01-31
```

The default database is `data/databases/stock_market.db`. Repeating a command
is safe: security mappings and daily prices are upserted transactionally, and
successful price dates are skipped. Failed exchange/date downloads are recorded
and make the command exit non-zero while other dates and sources continue.

## Data model

- `securities`: canonical company/security identity, preferring ISIN.
- `exchange_symbols`: raw and normalized NSE/BSE aliases, series, and BSE scrip
  codes linked to one canonical security.
- `daily_prices`: exchange/date/series OHLCV bhavcopy observations.
- `ingestion_runs`, `ingestion_checkpoints`, `ingestion_errors`: truthful run
  status, incremental progress, and source failures.
- `schema_migrations`: locally applied schema version.

Legacy ancillary scripts continue to produce ignored CSV files under
`data/raw/`. SQLite is the durable source of truth for equity masters and daily
prices. See [INSTALLATION.md](INSTALLATION.md) and [USAGE.md](USAGE.md).

## Source notes

NSE uses its official equity master CSV and legacy/UDiFF bhavcopy ZIP archives.
BSE uses its official active-scrip API and legacy/UDiFF equity bhavcopy ZIP
archives. Exchanges may omit archives on weekends and market holidays; narrow
explicit date ranges avoid recording expected non-trading days as failures.

## License

MIT License. Data remains subject to the exchanges' terms of use.

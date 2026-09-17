# Installation

## Requirements

- Python 3.9 or newer
- Internet access to `nseindia.com`, `nseindia.com` archive hosts, and
  `bseindia.com`
- SQLite (included with Python)

The pinned dependencies support the macOS system Python 3.9/LibreSSL stack,
including `urllib3` 1.26. The corrected `openpyxl` pin is `3.1.2`.

## macOS and Linux

```bash
git clone https://github.com/ABHIJITSINGH14/indian-stock-market-data.git
cd indian-stock-market-data
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/run_all.py init-db
```

On Apple Silicon, the standard python.org installer or Homebrew Python is
recommended if the older system Python cannot build an unrelated legacy
analysis dependency.

## Windows

```powershell
py -3.9 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/run_all.py init-db
```

Initialization creates `data/databases/stock_market.db` and applies all local
schema migrations. Override it with a SQLAlchemy SQLite URL:

```bash
python scripts/run_all.py init-db \
  --database-url sqlite:////absolute/path/to/market.db
```

## Verification

```bash
python -m unittest discover -s tests -v
python scripts/run_all.py --help
python -m scripts.run_all --help
```

Downloaded databases, CSVs, and logs are ignored by Git and must not be
committed.

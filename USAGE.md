# Usage Guide

## Quick Start

### Run All Downloads

```bash
python scripts/run_all.py
```

This runs all download tasks in sequence:
1. NSE Historical Data (20+ years)
2. BSE Company Data
3. Company Fundamentals
4. Bulk & Block Deals (last 90 days)
5. Corporate Actions

## Individual Downloads

### Download NSE Historical Data

```bash
python scripts/download_nse_data.py
```

**Output:** `data/raw/nse_bhavcopy.csv`

**Data includes:**
- Daily OHLCV (Open, High, Low, Close, Volume)
- 20+ years of historical data
- 20 major NSE stocks

**Customization:**
Edit `config/config.py` to modify:
```python
NSE_STOCKS = ['RELIANCE.NS', 'TCS.NS', ...]  # Add/remove stocks
START_DATE = TODAY - timedelta(days=365*20)  # Change time period
```

### Download BSE Company Data

```bash
python scripts/download_bse_data.py
```

**Output:** `data/raw/bse_data.csv`

**Data includes:**
- Listed company information
- Script codes
- ISIN numbers
- Company status

### Download Company Fundamentals

```bash
python scripts/download_fundamentals.py
```

**Output:** `data/raw/fundamentals.csv`

**Data includes:**
- Market cap
- P/E ratio
- Dividend yield
- Book value
- P/B ratio
- Revenue
- ROE
- Debt-to-equity ratio
- 52-week high/low

### Download Bulk & Block Deals

```bash
python scripts/bulk_block_deals.py
```

**Output:**
- `data/raw/bulk_deals.csv`
- `data/raw/block_deals.csv`

**Data includes:**
- Deal quantity
- Deal price
- Deal date
- Client/Counterparty info

### Download Corporate Actions

```bash
python scripts/corporate_actions.py
```

**Output:** `data/raw/corporate_actions.csv`

**Data includes:**
- Dividends
- Stock splits
- Bonus shares
- Rights issues
- Ex-dates

## Analyze Data

### Run Data Analysis

```bash
python scripts/analyze_data.py
```

**Generates:**
- Price trends analysis
- Fundamental analysis
- Data summary report

### Analysis Output

```
==================================================
DATA SUMMARY REPORT
==================================================

Price Data: 50000 records
  Symbols: 20
  Date range: 2004-01-01 to 2024-01-15

Fundamentals: 20 companies

Bulk Deals: 1500 records

Block Deals: 800 records

Corporate Actions: 2000 records
==================================================
```

## Accessing Downloaded Data

### CSV Files

All data is saved as CSV in `data/raw/` directory:

```python
import pandas as pd

# Read NSE data
df_nse = pd.read_csv('data/raw/nse_bhavcopy.csv')
print(df_nse.head())

# Read fundamentals
df_fund = pd.read_csv('data/raw/fundamentals.csv')
print(df_fund.head())
```

### Database (Optional)

For large datasets, use SQLite:

```python
import pandas as pd
from sqlalchemy import create_engine

engine = create_engine('sqlite:///data/databases/stock_market.db')

# Read from database
df = pd.read_sql('SELECT * FROM nse_bhavcopy', con=engine)
print(df.head())
```

## Scheduled Downloads

### Using GitHub Actions (Cloud)

Enable workflow in `.github/workflows/download-data.yml`:
1. Go to your repository
2. Click "Actions" tab
3. Enable workflow
4. It runs automatically every weekday at 5 AM IST

### Using Cron (Linux/Mac)

Edit crontab:
```bash
crontab -e
```

Add line to run daily at 4 AM:
```
0 4 * * * cd /path/to/indian-stock-market-data && python scripts/run_all.py
```

### Using Task Scheduler (Windows)

1. Open Task Scheduler
2. Create Basic Task
3. Set trigger (daily/weekly)
4. Set action: `python.exe scripts/run_all.py`
5. Set location: Project directory

## Configuration

### Edit config/config.py

```python
# Change date range
START_DATE = datetime(2020, 1, 1).date()  # From 2020
END_DATE = datetime.now().date()  # To today

# Add/remove stocks
NSE_STOCKS = [
    'RELIANCE.NS',
    'TCS.NS',
    'INFOSY.NS',
    # Add more...
]

# Change retry attempts
API_RETRY_ATTEMPTS = 5  # Default: 3

# Change request delay
REQUEST_DELAY = 1.0  # seconds between requests
```

## Logging

### View Logs

Logs are saved in `logs/` directory:

```bash
# View today's log
cat logs/app_20240115.log

# View last 100 lines
tail -100 logs/app_20240115.log

# Filter errors
grep ERROR logs/app_20240115.log
```

### Change Log Level

```python
# In config/config.py
LOG_LEVEL = 'DEBUG'  # Options: DEBUG, INFO, WARNING, ERROR, CRITICAL
```

## Troubleshooting

### Issue: "No data found for symbol"

**Solution:**
- Check if symbol is correct (e.g., 'RELIANCE.NS')
- Verify date range is valid
- Check internet connection

### Issue: "API timeout"

**Solution:**
- Increase timeout in config: `API_TIMEOUT = 60`
- Reduce number of stocks to download
- Check internet speed

### Issue: "Rate limit exceeded"

**Solution:**
- Increase request delay: `REQUEST_DELAY = 2.0`
- Run downloads at different times
- Use smaller date ranges

### Issue: "File already exists"

**Solution:**
- Backup existing file: `mv data/raw/nse_bhavcopy.csv data/raw/nse_bhavcopy.csv.bak`
- Run download again

## Performance Tips

1. **Run during off-market hours** to avoid rate limiting
2. **Use VPN** if blocked by ISP
3. **Increase request delay** for stability
4. **Download in batches** instead of all at once
5. **Monitor resource usage** for large datasets

## Data Analysis Examples

### Calculate Moving Averages

```python
import pandas as pd

df = pd.read_csv('data/raw/nse_bhavcopy.csv')
df['MA_50'] = df['close'].rolling(window=50).mean()
df['MA_200'] = df['close'].rolling(window=200).mean()
print(df.head())
```

### Find Top Performers

```python
df = pd.read_csv('data/raw/fundamentals.csv')
top_pe = df.nsmallest(5, 'pe_ratio')[['symbol', 'pe_ratio', 'dividend_yield']]
print("Stocks with lowest P/E ratios:")
print(top_pe)
```

### Analyze Bulk Deals

```python
df = pd.read_csv('data/raw/bulk_deals.csv')
df_grouped = df.groupby('symbol').sum()['quantity'].sort_values(ascending=False)
print("Most active bulk deal stocks:")
print(df_grouped.head(10))
```

## Support & Issues

For issues, questions, or feature requests:

1. Check existing [issues](https://github.com/ABHIJITSINGH14/indian-stock-market-data/issues)
2. Create new issue with details
3. Include:
   - Error message (from logs)
   - Python version
   - OS and system info
   - Steps to reproduce

## Contributing

Contributions welcome! Please:

1. Fork repository
2. Create feature branch
3. Make changes
4. Submit pull request

## Disclaimer

This tool is for educational and personal analysis only. Always verify data and consult financial advisors before making investment decisions.

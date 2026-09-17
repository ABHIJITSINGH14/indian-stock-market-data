# Indian Stock Market Data Automation

An automated tool for downloading and analyzing Indian stock market data from BSE/NSE, company fundamentals, and financial filings for personal study and analysis.

## Features

- **NSE Historical Data**: Download daily stock prices for 20+ years
- **BSE Data**: Company information and historical pricing
- **Company Fundamentals**: Financial ratios, P/E, dividend history from open sources
- **Bulk & Block Deals**: Trading activity data
- **Corporate Actions**: Stock splits, dividends, bonus information
- **Automated Scheduling**: Run downloads on a schedule
- **Data Storage**: CSV and SQLite database support
- **Error Handling**: Robust retry logic and error reporting

## Data Sources

- **yfinance**: Historical stock prices (Yahoo Finance)
- **NSE Official Website**: Official market data
- **BSE Official Website**: BSE listed companies
- **Public APIs**: Open-source financial data APIs

## Installation

1. Clone the repository:
```bash
git clone https://github.com/ABHIJITSINGH14/indian-stock-market-data.git
cd indian-stock-market-data
```

2. Create virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage

```bash
python scripts/run_all.py
```

## License

MIT License - Free for personal and educational purposes

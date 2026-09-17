# Installation Guide

## System Requirements

- Python 3.8 or higher
- pip (Python package manager)
- Internet connection
- ~2-5 GB disk space for historical data

## Installation Steps

### 1. Clone the Repository

```bash
git clone https://github.com/ABHIJITSINGH14/indian-stock-market-data.git
cd indian-stock-market-data
```

### 2. Create Virtual Environment (Recommended)

**On Linux/macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**On Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

### 3. Upgrade pip

```bash
pip install --upgrade pip
```

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

### 5. Verify Installation

```bash
python -c "import yfinance, pandas, requests; print('Installation successful!')"
```

### 6. (Optional) Install as Package

```bash
pip install -e .
```

## Troubleshooting Installation

### Issue: `pip install` fails

**Solution:**
- Upgrade pip: `pip install --upgrade pip`
- Try installing with no cache: `pip install --no-cache-dir -r requirements.txt`
- Install packages one by one if batch install fails

### Issue: Virtual environment activation fails

**Solution:**
- Make sure you're in the project directory
- Check Python path: `which python3` (Linux/Mac) or `where python` (Windows)
- Try creating venv again: `python -m venv venv --clear`

### Issue: Permission denied error

**Solution:**
- On Linux/Mac, prefix commands with `sudo` or use user-level install
- On Windows, run Command Prompt as Administrator

## Post-Installation Setup

### 1. Create Data Directories

The script automatically creates these directories:
- `data/raw/` - Raw downloaded data
- `data/processed/` - Processed data
- `data/databases/` - Database files
- `logs/` - Log files

### 2. Configure Settings (Optional)

Edit `config/config.py` to customize:
- Download date ranges
- Stock symbols to download
- Retry parameters
- Logging levels

### 3. First Run

```bash
python scripts/run_all.py
```

This will:
1. Download NSE historical data (20+ years)
2. Fetch BSE company data
3. Download company fundamentals
4. Fetch bulk and block deals (last 90 days)
5. Get corporate actions data

## Next Steps

- Read [USAGE.md](USAGE.md) for detailed usage instructions
- Check [README.md](README.md) for feature overview
- Review downloaded data in `data/raw/` directory
- Run analysis: `python scripts/analyze_data.py`

## Getting Help

If you encounter issues:

1. Check the log files in `logs/` directory
2. Review error messages carefully
3. Ensure internet connection is stable
4. Try running individual scripts to isolate the issue
5. Check GitHub issues: https://github.com/ABHIJITSINGH14/indian-stock-market-data/issues

## Uninstallation

```bash
# Deactivate virtual environment
deactivate

# Remove virtual environment
rm -rf venv  # Linux/Mac
rmdir venv   # Windows

# Delete the project folder
rm -rf indian-stock-market-data  # Linux/Mac
rmdir /s indian-stock-market-data  # Windows
```

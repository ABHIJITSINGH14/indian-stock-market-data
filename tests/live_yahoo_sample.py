"""Bounded LIVE integration check, separate from full-universe production collection."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ALLOWED_SYMBOLS = ('RELIANCE.NS', 'INFY.NS')
START = '2026-09-14'
END = '2026-09-19'  # exclusive; these five sessions were observed in the initial probe
FILES = ('nse_bhavcopy.csv', 'fundamentals.csv')
SCOPE = 'one_symbol_price_sample_and_current_fundamentals_not_full_pipeline'


def identity():
    return {'run_id': os.environ['GITHUB_RUN_ID'], 'commit_sha': os.environ['GITHUB_SHA']}


def produce(directory, symbol='RELIANCE.NS'):
    if symbol not in ALLOWED_SYMBOLS:
        raise ValueError('Unsupported bounded-sample identity')
    symbols = [symbol]
    from scripts.download_nse_data import NSEDataDownloader
    from scripts.download_fundamentals import FundamentalsDownloader
    import yfinance as yf
    # Surface provider exceptions instead of allowing the library to hide them.
    # No verbose authentication logging and no automatic retries are enabled.
    yf.config.debug.hide_exceptions = False
    directory.mkdir(parents=True, exist_ok=False)
    price = NSEDataDownloader(symbols, START, END, directory / FILES[0])
    fundamental = FundamentalsDownloader(symbols, directory / FILES[1])
    try:
        if not price.run():
            raise ValueError('Live price adapter failed; no successful sample manifest')
        time.sleep(2)
        if not fundamental.run():
            raise ValueError('Live fundamentals adapter failed; no successful sample manifest')
        manifest = {**identity(), 'synthetic': False, 'scope': SCOPE,
                    'provider': 'yahoo_finance', 'provider_version': yf.__version__,
                    'symbols': symbols, 'price_start_inclusive': START, 'price_end_exclusive': END,
                    'fundamentals_basis': 'current_snapshot_not_point_in_time_history',
                    'historical_completeness': 'not_verified',
                    'price_rows': len(price.all_data), 'fundamental_rows': len(fundamental.data),
                    'price_coverage': price.coverage, 'fundamentals_coverage': fundamental.coverage,
                    'sha256': {f: hashlib.sha256((directory / f).read_bytes()).hexdigest() for f in FILES}}
        (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2, allow_nan=False) + '\n')
        print(json.dumps(manifest, indent=2))
    except Exception as exc:
        diagnostic = {**identity(), 'status': 'failure', 'scope': SCOPE,
                      'exception_type': type(exc).__name__, 'message': str(exc)[:500],
                      'price_coverage': price.coverage, 'fundamentals_coverage': fundamental.coverage,
                      'fundamentals_response_metadata': fundamental.last_response_metadata}
        (directory / 'failure-diagnostics.json').write_text(json.dumps(diagnostic, indent=2))
        raise


def consume(directory, report, symbol='RELIANCE.NS'):
    if symbol not in ALLOWED_SYMBOLS:
        raise ValueError('Unsupported bounded-sample identity')
    symbols = [symbol]
    import pandas as pd
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('synthetic') is not False or manifest.get('scope') != SCOPE:
        raise ValueError('Expected explicitly scoped live sample, not synthetic or full-pipeline data')
    for key, expected in identity().items():
        if manifest.get(key) != expected:
            raise ValueError('Producer identity mismatch: ' + key)
    if manifest.get('provider') != 'yahoo_finance' or manifest.get('symbols') != symbols:
        raise ValueError('Unexpected source or issuer scope')
    if set(manifest.get('sha256', {})) != set(FILES):
        raise ValueError('Unexpected manifest files')
    for name in FILES:
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != manifest['sha256'][name]:
            raise ValueError('Data checksum mismatch: ' + name)
    prices = pd.read_csv(directory / FILES[0])
    fundamentals = pd.read_csv(directory / FILES[1])
    expected_dates = {f'2026-09-{d}' for d in range(14, 19)}
    if len(prices) != 5 or set(prices['date']) != expected_dates:
        raise ValueError('Unexpected live price sample dates/count')
    if len(fundamentals) != 1 or not (fundamentals['market_cap'] > 0).all():
        raise ValueError('Missing live fundamentals')
    if set(fundamentals['market_cap_currency']) != {'INR'}:
        raise ValueError('Unverified fundamentals quote currency')
    if symbol == 'INFY.NS' and not fundamentals['name'].fillna('').str.contains('Infosys', case=False).all():
        raise ValueError('Expected Infosys issuer name for the corrected request')
    for frame in (prices, fundamentals):
        if set(frame['provider_symbol']) != set(symbols) or set(frame['source']) != {'yahoo_finance'}:
            raise ValueError('Row provenance mismatch')
    for key in ('price_coverage', 'fundamentals_coverage'):
        coverage = manifest[key]
        if coverage['requested'] != symbols or coverage['returned'] != symbols or coverage['not_returned']:
            raise ValueError('Incomplete requested sample identities')
    env = dict(os.environ, SAMPLE_DATA_DIR=str(directory.resolve()))
    code = ('import os; from config import config; '
            'config.RAW_DATA_DIR = os.environ["SAMPLE_DATA_DIR"]; '
            'from scripts.analyze_data import DataAnalyzer; DataAnalyzer().run()')
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    header = ('LIVE YAHOO SAMPLE — NOT FULL EXCHANGE OR HISTORICAL COVERAGE\n'
              f'Source run: {manifest["run_id"]}\nSource commit: {manifest["commit_sha"]}\n'
              'Prices: 2026-09-14 through 2026-09-18. Fundamentals: current snapshot only.\n')
    text = header + result.stdout
    report.write_text(text, encoding='utf-8')
    print(text)
    if result.returncode or 'Price Data: 5 records' not in text or 'Fundamentals: 1 companies' not in text:
        raise ValueError('Actual analyzer failed to consume the live sample')
    if 'not loaded' in text.lower() or ' - ERROR - ' in text:
        raise ValueError('Analyzer logged an error or missing core data')
    print('LIVE SAMPLE PASSED; production all-source and historical completeness remain unverified')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['produce', 'consume'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--symbol', choices=ALLOWED_SYMBOLS, default='RELIANCE.NS')
    args = parser.parse_args()
    if args.action == 'produce':
        produce(args.directory, args.symbol)
    else:
        if args.report is None:
            parser.error('--report required for consume')
        consume(args.directory, args.report, args.symbol)

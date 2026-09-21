"""Synthetic artifact handoff test; never contributes data to a live download run."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

FIXTURES = {
    'nse_bhavcopy.csv': 'symbol,date,close\nCI_FIXTURE_ONLY,2020-01-01,100\nCI_FIXTURE_ONLY,2020-01-02,101\n',
    'fundamentals.csv': 'symbol,market_cap\nCI_FIXTURE_ONLY,1000000000\n',
}
ROOT = Path(__file__).resolve().parents[1]


def produce(directory):
    directory.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name, text in FIXTURES.items():
        content = text.encode('utf-8')
        (directory / name).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    manifest = {
        'synthetic': True, 'run_id': os.environ.get('GITHUB_RUN_ID', 'local'),
        'commit_sha': os.environ.get('GITHUB_SHA', 'local'), 'sha256': hashes,
    }
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Produced SYNTHETIC CI inputs, not live market data')


def consume(directory, report):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('synthetic') is not True:
        raise ValueError('Not a synthetic CI artifact')
    for key, env in [('run_id', 'GITHUB_RUN_ID'), ('commit_sha', 'GITHUB_SHA')]:
        if manifest.get(key) != os.environ.get(env, 'local'):
            raise ValueError(f'Producer identity mismatch: {key}')
    if set(manifest['sha256']) != set(FIXTURES):
        raise ValueError('Unexpected fixture files')
    for name in FIXTURES:
        actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        expected = hashlib.sha256(FIXTURES[name].encode()).hexdigest()
        if actual != manifest['sha256'][name] or actual != expected:
            raise ValueError(f'Fixture checksum mismatch: {name}')
    env = dict(os.environ, CI_FIXTURE_DIR=str(directory.resolve()))
    code = (
        'import os; from config import config; '
        'config.RAW_DATA_DIR = os.environ["CI_FIXTURE_DIR"]; '
        'from scripts.analyze_data import DataAnalyzer; DataAnalyzer().run()'
    )
    result = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
    )
    text = 'SYNTHETIC CI FIXTURE — NOT LIVE MARKET DATA\n' + result.stdout
    report.write_text(text, encoding='utf-8')
    print(text)
    if result.returncode:
        raise RuntimeError(f'Analyzer failed with exit code {result.returncode}')
    if 'Price Data: 2 records' not in text or 'Fundamentals: 1 companies' not in text:
        raise ValueError('Analyzer did not consume the expected fixtures')
    if 'not loaded' in text.lower() or ' - ERROR - ' in text:
        raise ValueError('Analyzer logged missing or invalid data')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['produce', 'consume'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.action == 'produce':
        produce(args.directory)
    else:
        if args.report is None:
            parser.error('--report is required for consume')
        consume(args.directory, args.report)

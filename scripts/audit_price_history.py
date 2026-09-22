"""Bounded historical requests through the real adapter, with strict exit status."""
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from scripts.download_nse_data import NSEDataDownloader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--symbols', nargs='+', default=['RELIANCE.NS'])
    parser.add_argument('--continue-after-row-rejection', action='store_true')
    args = parser.parse_args()
    if len(args.symbols) > 20:
        parser.error('Historical diagnostics are bounded to at most 20 explicit issuers')
    # An evidence run may not reuse output from a previous attempt.
    args.directory.mkdir(parents=True, exist_ok=False)
    collector = NSEDataDownloader(
        symbols=args.symbols, output_path=args.directory / 'prices.csv',
        continue_after_row_rejection=args.continue_after_row_rejection)
    result = {
        'kind': 'bounded_historical_diagnostic_not_full_production',
        'run_id': os.environ.get('GITHUB_RUN_ID'),
        'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
        'commit_sha': os.environ.get('GITHUB_SHA'),
        'observed_at_utc': datetime.now(timezone.utc).isoformat(),
        'requested_start': collector.start_date.isoformat(),
        'requested_end_exclusive': collector.end_date.isoformat(),
        'historical_completeness': 'not_verified',
        'collector_success': False,
    }
    try:
        result['collector_success'] = collector.run()
    except Exception as exc:
        result['exception_type'] = type(exc).__name__
    finally:
        result['coverage'] = collector.coverage
        text = json.dumps(result, indent=2, allow_nan=False) + '\n'
        (args.directory / 'audit-run.json').write_text(text, encoding='utf-8')
        print(text)
    return 0 if result['collector_success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

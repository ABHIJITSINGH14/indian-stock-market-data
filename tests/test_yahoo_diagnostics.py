"""Response shape diagnostics must remain separate from admitted financial data."""
import tempfile
import types
import unittest
from pathlib import Path

from test_yahoo_sources import load_adapter


class YahooDiagnosticsTests(unittest.TestCase):
    def test_partial_dictionary_diagnostics_contain_shape_not_invented_values(self):
        cls, provider = load_adapter('download_fundamentals.py', 'FundamentalsDownloader')
        provider.Ticker.return_value = types.SimpleNamespace(info={'symbol': 'RELIANCE.NS', 'currency': 'INR'})
        with tempfile.TemporaryDirectory() as temp:
            task = cls(symbols=['RELIANCE.NS'], output_path=Path(temp, 'fundamentals.csv'))
            self.assertFalse(task.run())
            metadata = task.last_response_metadata
            self.assertEqual(metadata['payload_type'], 'dict')
            self.assertEqual(metadata['field_count'], 2)
            self.assertIs(metadata['market_cap_present'], False)
            self.assertEqual(metadata['market_cap_type'], 'NoneType')
            self.assertIs(metadata['symbol_matches'], True)
            self.assertNotIn('marketCap', metadata)
            self.assertFalse(task.output_path.exists())


if __name__ == '__main__':
    unittest.main()

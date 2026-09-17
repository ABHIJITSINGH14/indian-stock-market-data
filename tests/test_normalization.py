import unittest

from market_data.normalization import canonical_security_id, normalize_isin, normalize_symbol


class NormalizationTests(unittest.TestCase):
    def test_yahoo_suffix_is_only_removed_for_yahoo_aliases(self):
        self.assertEqual(normalize_symbol(" reliance.ns ", "NSE", "yahoo"), "RELIANCE")
        self.assertEqual(normalize_symbol("RELIANCE.NS", "NSE", "official"), "RELIANCE.NS")

    def test_security_significant_punctuation_is_preserved(self):
        self.assertNotEqual(normalize_symbol("M&M"), normalize_symbol("MM"))

    def test_isin_identity_joins_exchanges(self):
        nse = canonical_security_id("INE002A01018", "NSE", "RELIANCE", "EQ")
        bse = canonical_security_id("ine002a01018", "BSE", "RELIANCE", "A", "500325")
        self.assertEqual(nse, bse)

    def test_missing_isin_fallback_is_deterministic_and_exchange_scoped(self):
        first = canonical_security_id(None, "NSE", "ABC", "EQ")
        second = canonical_security_id(None, "NSE", "ABC", "EQ")
        bse = canonical_security_id(None, "BSE", "ABC", "EQ")
        self.assertEqual(first, second)
        self.assertNotEqual(first, bse)
        self.assertIsNone(normalize_isin("not-an-isin"))

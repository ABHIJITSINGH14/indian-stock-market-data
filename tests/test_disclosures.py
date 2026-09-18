import tempfile
import unittest
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from market_data.database import MarketDatabase, securities
from market_data.disclosures import (
    BSEDisclosureCollector,
    NSEDisclosureCollector,
    board_meetings,
    corporate_actions,
    filings,
    financial_metrics,
    institutional_activity,
    market_metrics,
    shareholding_institutional_split,
    shareholding_patterns,
    _url,
    _date,
    DisclosureSourceError,
    NSEDisclosureClient,
)


SHAREHOLDING_XBRL = b"""<?xml version="1.0"?>
<xbrli:xbrl
 xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
 xmlns:shp="http://www.bseindia.com/xbrl/shp/2025-10-31/in-bse-shp"
 xmlns:xbrldt="http://xbrl.org/2005/xbrldt">
 <xbrli:context id="fpi">
  <xbrli:entity><xbrli:identifier scheme="isin">INE001A01036</xbrli:identifier>
   <xbrli:segment>
    <xbrldi:explicitMember dimension="shp:ShareholderCategoryAxis">
     shp:InstitutionsForeignPortfolioInvestorCategoryOneMember
    </xbrldi:explicitMember>
   </xbrli:segment>
  </xbrli:entity>
  <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period>
 </xbrli:context>
 <xbrli:context id="mf">
  <xbrli:entity><xbrli:identifier scheme="isin">INE001A01036</xbrli:identifier>
   <xbrli:segment>
    <xbrldi:explicitMember dimension="shp:ShareholderCategoryAxis">
     shp:MutualFundsOrUTIMember
    </xbrldi:explicitMember>
   </xbrli:segment>
  </xbrli:entity>
  <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period>
 </xbrli:context>
 <shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="fpi">0.17</shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
 <shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="mf">0.10</shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
</xbrli:xbrl>"""

FINANCIAL_XBRL = b"""<?xml version="1.0"?>
<xbrli:xbrl
 xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
 xmlns:fin="http://example.test/financial/2026">
 <xbrli:context id="quarter">
  <xbrli:entity><xbrli:identifier scheme="isin">INE123A01010</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
 </xbrli:context>
 <xbrli:context id="ytd">
  <xbrli:entity><xbrli:identifier scheme="isin">INE123A01010</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
 </xbrli:context>
 <xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>
 <fin:RevenueFromOperations contextRef="quarter" unitRef="INR">100</fin:RevenueFromOperations>
 <fin:RevenueFromOperations contextRef="ytd" unitRef="INR">280</fin:RevenueFromOperations>
</xbrli:xbrl>"""


class FakeClient:
    def index(self, dataset, start=None, end=None, symbol=None):
        if dataset == "corporate_actions":
            return [{
                "symbol": "RELIANCE", "isin": "INE002A01018",
                "recordId": "ca-1", "subject": "Dividend - Rs 10 Per Share",
                "exDate": "17-Sep-2026", "recDate": "18-Sep-2026",
            }]
        if dataset == "shareholding":
            return [{
                "symbol": "RELIANCE", "isin": "INE002A01018",
                "recordId": "sh-1", "date": "30-JUN-2026",
                "pr_and_prgrp": "50.48", "public_val": "49.52",
                "xbrl": "https://example.test/shareholding.xml",
            }]
        if dataset == "financial_results":
            return [{
                "symbol": "ABC", "isin": "INE123A01010",
                "seqNumber": "fin-1", "filingDate": "17-Sep-2026",
                "fromDate": "01-Apr-2026", "toDate": "30-Jun-2026",
                "consolidated": "Consolidated",
                "xbrl": "https://example.test/financial.xml",
            }]
        if dataset == "fii_dii":
            return [
                {
                    "date": "17-Sep-2026", "category": "DII",
                    "buyValue": "14105.01", "sellValue": "10487.26",
                    "netValue": "3617.75",
                },
                {
                    "date": "17-Sep-2026", "category": "FII/FPI",
                    "buyValue": "8761.71", "sellValue": "11970.47",
                    "netValue": "-3208.76",
                },
            ]
        return []

    def document(self, url):
        return (
            FINANCIAL_XBRL if url.endswith("financial.xml") else SHAREHOLDING_XBRL,
            "application/xml",
        )


class FakeBSEClient:
    def index(self, dataset, scrip_code, start=None, end=None):
        if dataset == "board_meetings":
            return {"Table": [{
                "scrip_code": scrip_code,
                "Short_name": "RELIANCE",
                "LONG_NAME": "Reliance Industries Ltd",
                "Purpose_name": "Results",
                "meeting_date": "17 Jul 2026",
                "tm": "2026-07-17T00:00:00",
            }]}
        if dataset == "financial_results":
            return {"Data": "<table><tr><td>Official result index</td></tr></table>"}
        return {"Table": []}


class BrokenDocumentClient(FakeClient):
    def document(self, url):
        raise RuntimeError("missing exchange document")


class CircuitResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.content = b"{}"
        self.headers = {}
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP {}".format(self.status_code))

    def json(self):
        return self._payload


class CircuitSession:
    def __init__(self):
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return CircuitResponse(403 if self.calls == 1 else 429)


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "market.db"
        self.database = MarketDatabase("sqlite:///{}".format(path))
        self.database.initialize()

    def tearDown(self):
        self.database.engine.dispose()
        self.temp.cleanup()

    def test_shareholding_split_is_taxonomy_version_independent(self):
        fii, dii = shareholding_institutional_split(SHAREHOLDING_XBRL)
        self.assertEqual(fii, 17.0)
        self.assertEqual(dii, 10.0)

    def test_exchange_url_sentinels_are_not_downloaded(self):
        self.assertIsNone(_url("-"))
        self.assertIsNone(_url("N/A"))
        self.assertEqual(
            _url("/corporate/example.xml"),
            "https://www.nseindia.com/corporate/example.xml",
        )
        self.assertIsNone(_url("https://nsearchives.nseindia.com/corporate/xbrl/-"))

    def test_bse_iso_timestamp_is_parsed_as_filing_date(self):
        self.assertEqual(
            _date("2018-07-27T20:24:53.0"), date(2018, 7, 27)
        )

    def test_collection_is_idempotent_and_keyed_to_security(self):
        collector = NSEDisclosureCollector(self.database, FakeClient())
        requested = ("corporate_actions", "shareholding", "fii_dii")
        first = collector.collect(
            date(2026, 9, 11), date(2026, 9, 17), requested, window_days=30
        )
        second = collector.collect(
            date(2026, 9, 11), date(2026, 9, 17), requested, window_days=30
        )
        self.assertEqual(first, second)
        with self.database.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(func.count()).select_from(corporate_actions)).scalar_one(),
                1,
            )
            holding = connection.execute(select(shareholding_patterns)).mappings().one()
            self.assertIsNotNone(holding["security_id"])
            self.assertEqual(holding["symbol"], "RELIANCE")
            self.assertEqual(holding["fii_percent"], 17.0)
            self.assertEqual(holding["dii_percent"], 10.0)
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(institutional_activity)
                ).scalar_one(),
                2,
            )

    def test_index_only_filing_is_enriched_instead_of_duplicated(self):
        index_collector = NSEDisclosureCollector(
            self.database, FakeClient(), fetch_documents=False
        )
        full_collector = NSEDisclosureCollector(
            self.database, FakeClient(), fetch_documents=True
        )
        requested = ("shareholding",)
        index_collector.collect(
            date(2026, 9, 11), date(2026, 9, 17), requested
        )
        full_collector.collect(
            date(2026, 9, 11), date(2026, 9, 17), requested
        )
        with self.database.engine.connect() as connection:
            count = connection.execute(
                select(func.count()).select_from(filings).where(
                    filings.c.external_id == "sh-1"
                )
            ).scalar_one()
        self.assertEqual(count, 1)

    def test_quarterly_metrics_are_not_overwritten_by_ytd_facts(self):
        collector = NSEDisclosureCollector(self.database, FakeClient())
        collector.collect(
            date(2026, 9, 11),
            date(2026, 9, 17),
            ("financial_results",),
        )
        with self.database.engine.connect() as connection:
            revenue = connection.execute(
                select(financial_metrics.c.value).where(
                    financial_metrics.c.symbol == "ABC",
                    financial_metrics.c.metric == "revenue",
                )
            ).scalar_one()
        self.assertEqual(revenue, 100.0)

    def test_invalid_activity_net_is_rejected(self):
        collector = NSEDisclosureCollector(self.database, FakeClient())
        with self.assertRaisesRegex(ValueError, "net value"):
            collector._activity([{
                "date": "17-Sep-2026", "category": "DII",
                "buyValue": "10", "sellValue": "2", "netValue": "99",
            }])

    def test_document_failure_is_recorded_and_batch_continues(self):
        collector = NSEDisclosureCollector(self.database, BrokenDocumentClient())
        counts = collector.collect(
            date(2026, 9, 11),
            date(2026, 9, 17),
            ("shareholding", "corporate_actions"),
        )
        self.assertEqual(counts["shareholding"], 0)
        self.assertEqual(counts["shareholding_failed"], 1)
        self.assertEqual(counts["corporate_actions"], 1)

    def test_nse_circuit_breaker_opens_after_rate_limit(self):
        session = CircuitSession()
        clock = lambda: 100.0
        client = NSEDisclosureClient(
            session=session,
            delay=0,
            retries=0,
            circuit_breaker_threshold=1,
            sleep=lambda _: None,
            clock=clock,
        )
        with self.assertRaisesRegex(DisclosureSourceError, "HTTP 429"):
            client.index(
                "financial_results", date(2026, 1, 1), date(2026, 1, 2)
            )
        calls = session.calls
        with self.assertRaisesRegex(DisclosureSourceError, "circuit breaker"):
            client.index(
                "financial_results", date(2026, 1, 1), date(2026, 1, 2)
            )
        self.assertEqual(session.calls, calls)

    def test_bse_records_join_by_isin_or_scrip_mapping(self):
        self.database.upsert_securities([{
            "exchange": "BSE", "symbol": "RELIANCE", "scrip_code": "500325",
            "isin": "INE002A01018", "name": "Reliance Industries Ltd",
            "source": "test",
        }])
        collector = BSEDisclosureCollector(self.database, FakeBSEClient())
        counts = collector.collect(
            ("board_meetings",), ("500325",),
            start=date(2026, 7, 1), end=date(2026, 7, 31),
        )
        self.assertEqual(counts["board_meetings"], 1)
        with self.database.engine.connect() as connection:
            row = connection.execute(select(board_meetings)).mappings().one()
            self.assertEqual(row["symbol"], "RELIANCE")
            self.assertIsNotNone(row["security_id"])

    def test_legacy_bse_html_result_index_is_preserved_without_pdf_parsing(self):
        self.database.upsert_securities([{
            "exchange": "BSE", "symbol": "RELIANCE", "scrip_code": "500325",
            "isin": "INE002A01018", "name": "Reliance Industries Ltd",
            "source": "test",
        }])
        counts = BSEDisclosureCollector(
            self.database, FakeBSEClient()
        ).collect(("financial_results",), ("500325",))

        self.assertEqual(counts["financial_results"], 1)
        with self.database.engine.connect() as connection:
            filing = connection.execute(
                select(filings).where(filings.c.exchange == "BSE")
            ).mappings().one()
            metric_count = connection.scalar(
                select(func.count()).select_from(financial_metrics)
            )
        self.assertIn("Official result index", filing["raw_json"])
        self.assertEqual(filing["document_sha256"], "")
        self.assertEqual(metric_count, 0)

    def test_market_metrics_are_refreshed_from_prices(self):
        self.database.upsert_prices([{
            "exchange": "NSE", "symbol": "ABC", "series": "EQ",
            "isin": "INE123A01010", "name": "ABC Limited",
            "trading_date": date(2026, 9, 17), "open": 9.0,
            "high": 12.0, "low": 8.0, "close": 10.0,
            "source": "fixture",
        }])
        collector = NSEDisclosureCollector(self.database, FakeClient())
        collector.store.initialize()
        collector.refresh_market_metrics()
        with self.database.engine.connect() as connection:
            values = {
                row.metric: row.value
                for row in connection.execute(
                    select(market_metrics).where(market_metrics.c.symbol == "ABC")
                )
            }
        self.assertEqual(values["price"], 10.0)
        self.assertEqual(values["high_52w"], 12.0)
        self.assertEqual(values["low_52w"], 8.0)

    def test_late_isin_merge_moves_existing_disclosure_foreign_keys(self):
        collector = BSEDisclosureCollector(self.database, FakeBSEClient())
        collector.collect(
            ("board_meetings",), ("500325",),
            start=date(2026, 7, 1), end=date(2026, 7, 31),
        )
        self.database.upsert_securities([{
            "exchange": "NSE", "symbol": "RELIANCE", "series": "EQ",
            "isin": "INE002A01018", "name": "Reliance Industries Limited",
            "source": "nse_master",
        }])
        self.database.upsert_securities([{
            "exchange": "BSE", "symbol": "RELIANCE", "series": "",
            "scrip_code": "500325", "isin": "INE002A01018",
            "name": "Reliance Industries Limited", "source": "bse_master",
        }])
        with self.database.engine.connect() as connection:
            filing_security = connection.scalar(select(filings.c.security_id))
            canonical_security = connection.scalar(
                select(securities.c.id).where(securities.c.isin == "INE002A01018")
            )
        self.assertEqual(filing_security, canonical_security)


if __name__ == "__main__":
    unittest.main()

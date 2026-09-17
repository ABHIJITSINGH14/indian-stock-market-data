import json
import unittest
from datetime import date
from decimal import Decimal

import requests

from market_disclosures import (
    BSEClient,
    DocumentFormatError,
    ExchangeTransport,
    NSEClient,
    RawDocument,
    aggregate_shareholding,
    iter_date_windows,
    parse_xbrl,
)
from market_disclosures.models import DisclosureError


class FakeResponse:
    def __init__(self, payload, status=200, content_type="application/json", url="https://x/api"):
        self.status_code = status
        self.url = url
        self.headers = {"Content-Type": content_type}
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    @property
    def text(self):
        return self.content.decode(errors="replace")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        response.url = url
        return response

    def close(self):
        pass


def transport(*responses, max_retries=0, sleeps=None):
    return ExchangeTransport(
        session=FakeSession(responses), min_interval=0, max_retries=max_retries,
        backoff_base=0.1, jitter=0, sleep=(sleeps if sleeps is not None else []).append,
    )


XBRL = b"""<?xml version="1.0"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:t="urn:taxonomy:v99"
 xmlns:d="urn:dimensions:v2" xmlns:iso4217="urn:iso:std:iso:4217">
 <xbrli:context id="domestic">
  <xbrli:entity><xbrli:identifier scheme="x">ABC</xbrli:identifier>
   <xbrli:segment><xbrldi:explicitMember dimension="d:CategoryAxis">d:InstitutionsDomesticMember</xbrldi:explicitMember></xbrli:segment>
  </xbrli:entity><xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period>
 </xbrli:context>
 <xbrli:context id="foreign1"><xbrli:entity><xbrli:identifier scheme="x">ABC</xbrli:identifier>
  <xbrli:segment><xbrldi:explicitMember dimension="d:CategoryAxis">d:InstitutionsForeignPortfolioInvestorCategoryOneMember</xbrldi:explicitMember></xbrli:segment>
 </xbrli:entity><xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:context id="foreign2"><xbrli:entity><xbrli:identifier scheme="x">ABC</xbrli:identifier>
  <xbrli:segment><xbrldi:explicitMember dimension="d:CategoryAxis">d:InstitutionsForeignPortfolioInvestorCategoryTwoMember</xbrldi:explicitMember></xbrli:segment>
 </xbrli:entity><xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
 <t:ShareholdingPercentage contextRef="domestic" unitRef="pure" decimals="4">0.1200</t:ShareholdingPercentage>
 <t:ShareholdingPercentage contextRef="foreign1" unitRef="pure" decimals="4">0.1000</t:ShareholdingPercentage>
 <t:ShareholdingPercentage contextRef="foreign2" unitRef="pure" decimals="4">0.0500</t:ShareholdingPercentage>
</xbrli:xbrl>"""


class TransportTests(unittest.TestCase):
    def test_short_date_windows_are_inclusive(self):
        windows = list(
            iter_date_windows(date(2026, 1, 1), date(2026, 1, 8), window_days=3)
        )
        self.assertEqual(
            windows,
            [
                (date(2026, 1, 1), date(2026, 1, 3)),
                (date(2026, 1, 4), date(2026, 1, 6)),
                (date(2026, 1, 7), date(2026, 1, 8)),
            ],
        )

    def test_raw_document_and_json(self):
        t = transport(FakeResponse([{"x": 1}]))
        doc, value = t.get("https://x/api")
        self.assertIsInstance(doc, RawDocument)
        self.assertEqual(value[0]["x"], 1)
        self.assertEqual(len(doc.sha256), 64)

    def test_html_masquerading_as_json_or_xml_is_rejected(self):
        html = FakeResponse(b"<html>blocked</html>", content_type="application/json")
        with self.assertRaises(DocumentFormatError):
            transport(html).get("https://x/api")
        html2 = FakeResponse(b"<!doctype html><title>blocked</title>", content_type="text/html")
        with self.assertRaises(DocumentFormatError):
            transport(html2).get("https://x/file", expected="xml")

    def test_retries_bounded_status_and_exception(self):
        sleeps = []
        t = transport(
            requests.ConnectionError("offline"), FakeResponse({}, 429), FakeResponse([]),
            max_retries=2, sleeps=sleeps,
        )
        _, value = t.get("https://x/api")
        self.assertEqual(value, [])
        self.assertEqual(sleeps, [0.1, 0.2])
        self.assertEqual(len(t.session.calls), 3)


class XbrlTests(unittest.TestCase):
    def test_context_qname_metadata_and_category_aggregation(self):
        parsed = parse_xbrl(XBRL)
        fact = parsed.facts[0]
        self.assertEqual(fact.local_name, "ShareholdingPercentage")
        self.assertEqual(fact.namespace, "urn:taxonomy:v99")
        self.assertEqual(fact.unit_id, "pure")
        self.assertEqual(fact.decimals, "4")
        self.assertIn("d:CategoryAxis", fact.dimensions)
        result = aggregate_shareholding(parsed, Decimal("55.00"))
        self.assertEqual(result["dii_percentage"], Decimal("12.0000"))
        self.assertEqual(result["dii_aggregation"], "parent")
        self.assertEqual(result["fii_fpi_percentage"], Decimal("15.0000"))
        self.assertEqual(result["fii_fpi_aggregation"], "children")
        self.assertEqual(result["official_public_percentage"], Decimal("55.00"))


class NSETests(unittest.TestCase):
    def test_bootstrap_403_is_tolerated(self):
        session = FakeSession(
            [FakeResponse(b"blocked", status=403, content_type="text/plain")]
        )
        client = NSEClient(
            transport=ExchangeTransport(session=session, min_interval=0)
        )
        self.assertIsNotNone(client)
        self.assertEqual(len(session.calls), 1)

    def test_shareholding_preserves_revision_and_official_public(self):
        payload = [{
            "symbol": "ABC", "name": "ABC Ltd", "date": "30-JUN-2026",
            "pr_and_prgrp": "42.50", "public_val": "57.50", "employeeTrusts": "0",
            "application_no": "F1", "revisionDate": "2026-07-04", "xbrl": "/a.xml",
        }]
        client = NSEClient(transport=transport(FakeResponse(payload)), bootstrap=False)
        result = client.shareholdings(date(2026, 1, 1), date(2026, 1, 7))
        self.assertEqual(result.records[0]["filing_id"], "F1")
        self.assertEqual(result.records[0]["revision_id"], "2026-07-04")
        self.assertEqual(result.records[0]["public_percentage"], Decimal("57.50"))
        self.assertNotIn("fii_fpi_percentage", result.records[0])

    def test_linked_xbrl_is_parsed_and_archived(self):
        payload = [{"symbol": "ABC", "public_val": "55", "xbrl": "https://x/a.xml"}]
        t = transport(
            FakeResponse(payload),
            FakeResponse(XBRL, content_type="application/xml"),
        )
        result = NSEClient(transport=t, bootstrap=False).shareholdings(
            date(2026, 1, 1), date(2026, 1, 2), parse_linked_xbrl=True
        )
        self.assertEqual(result.records[0]["institutional"]["dii_percentage"], Decimal("12.00"))
        self.assertEqual(len(result.linked_documents), 1)

    def test_empty_envelopes_and_endpoint_normalizers(self):
        responses = [
            FakeResponse({"data": [], "msg": "No Data Found"}),
            FakeResponse({"data": None}),
            FakeResponse({"acqNameList": [], "data": []}),
        ]
        client = NSEClient(transport=transport(*responses), bootstrap=False)
        start, end = date(2026, 1, 1), date(2026, 1, 2)
        self.assertEqual(client.board_meetings(start, end).records, [])
        self.assertEqual(client.pit(start, end).records, [])
        self.assertEqual(client.sast_reg29(start, end).records, [])

    def test_all_primary_normalizers(self):
        responses = [
            FakeResponse([{"symbol": "ABC", "subject": "Dividend", "faceVal": "10"}]),
            FakeResponse([{"bm_symbol": "ABC", "bm_purpose": "Notice", "bm_desc": "Long meeting description"}]),
            FakeResponse({"data": [{"symbol": "ABC", "appId": "P1", "prevAppId": "P0", "xmlFileName": "/p.xml"}]}),
            FakeResponse({"data": [{"symbol": "ABC", "application_no": "S1", "noOfShareAcq": "1,000", "totAftShare": "4.2"}]}),
        ]
        client = NSEClient(transport=transport(*responses), bootstrap=False)
        start, end = date(2026, 1, 1), date(2026, 1, 2)
        self.assertEqual(client.corporate_actions(start, end).records[0]["purpose"], "Dividend")
        self.assertEqual(client.board_meetings(start, end).records[0]["details"], "Long meeting description")
        self.assertEqual(client.pit(start, end).records[0]["revision_id"], "P0")
        self.assertEqual(client.sast_reg29(start, end).records[0]["shares_acquired"], Decimal("1000"))

    def test_fii_dii_decimal_validation(self):
        good = [{"category": "DII", "date": "17-Sep-2026", "buyValue": "14,105.01",
                 "sellValue": "10487.26", "netValue": "3617.75"}]
        record = NSEClient(transport=transport(FakeResponse(good)), bootstrap=False).fii_dii().records[0]
        self.assertEqual(record["net_value"], Decimal("3617.75"))
        bad = [{"category": "DII", "date": "x", "buyValue": "10", "sellValue": "2", "netValue": "9"}]
        with self.assertRaises(DisclosureError):
            NSEClient(transport=transport(FakeResponse(bad)), bootstrap=False).fii_dii()

    def test_duplicate_ids_are_not_deduplicated(self):
        payload = [{"symbol": "ABC", "appId": "1", "revisionRemark": "original"},
                   {"symbol": "ABC", "appId": "1", "revisionRemark": "revised"}]
        result = NSEClient(transport=transport(FakeResponse({"data": payload})), bootstrap=False).pit(
            date(2026, 1, 1), date(2026, 1, 2)
        )
        self.assertEqual(len(result.records), 2)
        self.assertNotEqual(result.records[0]["revision_id"], result.records[1]["revision_id"])


class BSETests(unittest.TestCase):
    def test_response_variants_and_numeric_scripcode(self):
        responses = [
            FakeResponse({}),
            FakeResponse({"Table": []}),
            FakeResponse({"Table": [{"Fld_ID": 7, "Fld_ScripCode": "500325",
                                     "Fld_PromoterName": "Person", "xbrlurl": "/x.html"}]}),
            FakeResponse({"Table2": [{"scrip_code": "500325", "purpose": "Dividend", "Ex_date": "1 Jan 2026"}],
                          "Table": [{"purpose_name": "duplicate"}]}),
            FakeResponse({"Table": [{"scrip_code": "500325", "shareholdername": "Holder", "flag": "29(2) "}]}),
        ]
        client = BSEClient(transport=transport(*responses))
        start, end = date(2026, 1, 1), date(2026, 1, 2)
        self.assertEqual(client.board_meetings(500325, start, end).records, [])
        self.assertEqual(client.insider_trades(500325, start, end).records, [])
        self.assertEqual(client.insider_trades(500325, start, end).records[0]["filing_id"], "7")
        self.assertEqual(len(client.corporate_actions(500325, start, end).records), 1)
        self.assertEqual(client.sast(500325, start, end).records[0]["regulation"], "29(2)")
        with self.assertRaises(ValueError):
            client.sast("500325", start, end)


if __name__ == "__main__":
    unittest.main()

from datetime import date
from decimal import Decimal

import pytest

from fundamentals import (
    ContextQuery,
    FilingIdentity,
    MetricMapper,
    MetricSource,
    StatementScope,
    XBRLParseError,
    parse_xbrl,
)


XBRL = b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl
 xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
 xmlns:in="http://example.test/indas/2025"
 xmlns:old="http://example.test/indas/2024"
 xmlns:axis="http://example.test/axis">
  <xbrli:context id="cq-consolidated">
    <xbrli:entity>
      <xbrli:identifier scheme="https://www.mca.gov.in/CIN">L12345MH2000PLC1</xbrli:identifier>
      <xbrli:segment>
        <xbrldi:explicitMember dimension="axis:StatementAxis">axis:ConsolidatedMember</xbrldi:explicitMember>
      </xbrli:segment>
    </xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:context id="cq-standalone">
    <xbrli:entity>
      <xbrli:identifier scheme="https://www.mca.gov.in/CIN">L12345MH2000PLC1</xbrli:identifier>
      <xbrli:segment>
        <xbrldi:explicitMember dimension="axis:StatementAxis">axis:StandaloneMember</xbrldi:explicitMember>
      </xbrli:segment>
    </xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:context id="pq-consolidated">
    <xbrli:entity><xbrli:identifier scheme="https://www.mca.gov.in/CIN">L12345MH2000PLC1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2024-06-30</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:context id="ytd-consolidated">
    <xbrli:entity><xbrli:identifier scheme="https://www.mca.gov.in/CIN">L12345MH2000PLC1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:context id="instant-current">
    <xbrli:entity><xbrli:identifier scheme="https://www.mca.gov.in/CIN">L12345MH2000PLC1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2025-06-30</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>
  <xbrli:unit id="INR-per-share">
    <xbrli:divide>
      <xbrli:unitNumerator><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unitNumerator>
      <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator>
    </xbrli:divide>
  </xbrli:unit>
  <in:RevenueFromOperations contextRef="cq-consolidated" unitRef="INR" decimals="-3">1,000</in:RevenueFromOperations>
  <old:RevenueFromOperations contextRef="cq-consolidated" unitRef="INR" decimals="-3">999</old:RevenueFromOperations>
  <in:ProfitBeforeTax contextRef="cq-consolidated" unitRef="INR" decimals="0">100</in:ProfitBeforeTax>
  <in:FinanceCosts contextRef="cq-consolidated" unitRef="INR" decimals="0">20</in:FinanceCosts>
  <in:DepreciationAndAmortisationExpense contextRef="cq-consolidated" unitRef="INR" decimals="0">30</in:DepreciationAndAmortisationExpense>
  <in:ProfitLossForPeriod contextRef="cq-consolidated" unitRef="INR" decimals="0">75</in:ProfitLossForPeriod>
  <in:RevenueFromOperations contextRef="cq-standalone" unitRef="INR" decimals="0">800</in:RevenueFromOperations>
  <in:RevenueFromOperations contextRef="pq-consolidated" unitRef="INR" decimals="0">700</in:RevenueFromOperations>
  <in:RevenueFromOperations contextRef="ytd-consolidated" unitRef="INR" decimals="0">2800</in:RevenueFromOperations>
  <in:Assets contextRef="instant-current" unitRef="INR" decimals="INF">5000</in:Assets>
  <in:Narrative contextRef="cq-consolidated">not numeric</in:Narrative>
</xbrli:xbrl>
"""


def identity() -> FilingIdentity:
    return FilingIdentity(
        symbol="TEST",
        filing_date=date(2025, 7, 20),
        source_url="https://example.test/result.xml",
        sequence_number="42",
        consolidated=True,
    )


def test_parser_preserves_context_namespace_units_and_duplicate_facts():
    parsed = parse_xbrl(XBRL, identity())

    revenues = [fact for fact in parsed.facts if fact.local_name == "RevenueFromOperations"]
    assert len(revenues) == 5
    assert revenues[0].namespace == "http://example.test/indas/2025"
    assert revenues[0].numeric_value == Decimal("1000")
    assert revenues[0].decimals == "-3"
    assert revenues[0].unit_ref == "INR"
    assert parsed.contexts[0].entity_identifier == "L12345MH2000PLC1"
    assert parsed.contexts[0].dimensions[0].member_name == "ConsolidatedMember"
    assert parsed.units[1].denominator_measures == ("xbrli:shares",)


def test_mapper_selects_scope_period_and_derives_with_provenance():
    parsed = parse_xbrl(XBRL, identity())
    metrics = {
        metric.name: metric
        for metric in MetricMapper().map_filing(
            parsed,
            ContextQuery(
                period_end=date(2025, 6, 30),
                period="quarter",
                scope=StatementScope.CONSOLIDATED,
            ),
        )
    }

    assert metrics["revenue"].value == Decimal("1000")
    assert metrics["pat"].value == Decimal("75")
    assert metrics["ebitda"].value == Decimal("150")
    assert metrics["ebitda"].source == MetricSource.DERIVED
    assert metrics["ebitda"].derivation == (
        "pbt + finance_cost + depreciation_amortisation"
    )
    assert len(metrics["ebitda"].concept_qnames) == 3


def test_mapper_distinguishes_standalone_ytd_instant_and_prior():
    parsed = parse_xbrl(XBRL, identity())
    mapper = MetricMapper()

    standalone = mapper.map_filing(
        parsed,
        ContextQuery(
            period_end=date(2025, 6, 30),
            period="quarter",
            scope=StatementScope.STANDALONE,
        ),
    )
    ytd = mapper.map_filing(
        parsed,
        ContextQuery(period_end=date(2025, 12, 31), period="year_to_date"),
    )
    instant = mapper.map_filing(
        parsed,
        ContextQuery(period_end=date(2025, 6, 30), period="instant"),
    )
    prior = mapper.map_filing(parsed, ContextQuery(period="quarter", current=False))

    assert {item.name: item.value for item in standalone}["revenue"] == Decimal("800")
    assert {item.name: item.value for item in ytd}["revenue"] == Decimal("2800")
    assert {item.name: item.value for item in instant}["assets"] == Decimal("5000")
    assert {item.name: item.value for item in prior}["revenue"] == Decimal("700")


def test_parser_rejects_dtd_and_unknown_context():
    unsafe = b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'
    with pytest.raises(XBRLParseError, match="not allowed"):
        parse_xbrl(unsafe, identity())

    unknown = XBRL.replace(
        b'contextRef="cq-consolidated"',
        b'contextRef="does-not-exist"',
        1,
    )
    with pytest.raises(XBRLParseError, match="unknown context"):
        parse_xbrl(unknown, identity())

    original = parse_xbrl(XBRL, identity())
    tolerant = parse_xbrl(unknown, identity(), ignore_unknown_contexts=True)
    assert len(tolerant.facts) == len(original.facts) - 1
    assert tolerant.unknown_context_fact_count == 1
    assert tolerant.unknown_context_ids == ("does-not-exist",)

    legacy = XBRL.replace(b"not numeric", b"company\x92s")
    with pytest.raises(XBRLParseError, match="UTF-8"):
        parse_xbrl(legacy, identity())
    repaired = parse_xbrl(legacy, identity(), allow_legacy_encoding=True)
    assert repaired.source_encoding_repair == "windows-1252"
    assert repaired.facts[-1].value == "company\u2019s"

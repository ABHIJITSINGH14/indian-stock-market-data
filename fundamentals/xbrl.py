"""Secure, taxonomy-independent extraction of facts from XBRL instances."""

from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Dict, Iterable, List, Optional, Tuple

from lxml import etree

from .domain import (
    ContextRecord,
    Dimension,
    FactRecord,
    FilingIdentity,
    ParsedFiling,
    PeriodKind,
    UnitRecord,
)

XBRLI_NS = "http://www.xbrl.org/2003/instance"
XBRLDI_NS = "http://xbrl.org/2006/xbrldi"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_STRUCTURAL_NAMES = {
    "context",
    "unit",
    "schemaRef",
    "linkbaseRef",
    "footnoteLink",
    "roleRef",
    "arcroleRef",
}


class XBRLParseError(ValueError):
    """Raised when an XBRL instance is unsafe or structurally invalid."""


def parse_xbrl(
    xml: bytes,
    identity: FilingIdentity,
    ignore_unknown_contexts: bool = False,
    allow_legacy_encoding: bool = False,
) -> ParsedFiling:
    """Parse an XBRL instance without resolving entities, DTDs, or networks."""
    if not isinstance(xml, bytes):
        raise TypeError("xml must be bytes")
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise XBRLParseError("DTD and entity declarations are not allowed")

    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        recover=False,
        huge_tree=False,
        remove_comments=True,
    )
    source_encoding_repair = None
    try:
        root = etree.parse(BytesIO(xml), parser).getroot()
    except (etree.XMLSyntaxError, ValueError) as exc:
        if not allow_legacy_encoding or "UTF-8" not in str(exc).upper():
            raise XBRLParseError("invalid XBRL XML: {}".format(exc)) from exc
        try:
            repaired = xml.decode("windows-1252").encode("utf-8")
            root = etree.parse(BytesIO(repaired), parser).getroot()
        except (UnicodeError, etree.XMLSyntaxError, ValueError) as repair_exc:
            raise XBRLParseError(
                "invalid XBRL XML after Windows-1252 repair: {}".format(repair_exc)
            ) from repair_exc
        source_encoding_repair = "windows-1252"

    contexts = tuple(_parse_context(node) for node in root.findall(_x("context")))
    context_by_id = {context.context_id: context for context in contexts}
    units = tuple(_parse_unit(node) for node in root.findall(_x("unit")))
    facts: List[FactRecord] = []
    unknown_context_fact_count = 0
    unknown_context_ids = set()

    for node in root.iterchildren():
        local_name = etree.QName(node).localname
        if local_name in _STRUCTURAL_NAMES:
            continue
        context_ref = node.get("contextRef")
        if not context_ref:
            continue
        try:
            context = context_by_id[context_ref]
        except KeyError as exc:
            if ignore_unknown_contexts:
                unknown_context_fact_count += 1
                unknown_context_ids.add(context_ref)
                continue
            raise XBRLParseError(
                "fact {} references unknown context {}".format(local_name, context_ref)
            ) from exc
        text = "".join(node.itertext()).strip()
        nil = node.get("{{{}}}nil".format(XSI_NS), "false").lower() in ("true", "1")
        facts.append(
            FactRecord(
                filing=identity,
                context=context,
                namespace=etree.QName(node).namespace or "",
                local_name=local_name,
                value=text,
                numeric_value=None if nil else _decimal_or_none(text),
                unit_ref=node.get("unitRef"),
                decimals=node.get("decimals"),
                precision=node.get("precision"),
                nil=nil,
            )
        )

    return ParsedFiling(
        identity,
        contexts,
        units,
        tuple(facts),
        unknown_context_fact_count,
        tuple(sorted(unknown_context_ids)),
        source_encoding_repair,
    )


def _x(local_name: str) -> str:
    return "{{{}}}{}".format(XBRLI_NS, local_name)


def _parse_date(text: Optional[str], label: str) -> date:
    if not text:
        raise XBRLParseError("context is missing {}".format(label))
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError as exc:
        raise XBRLParseError("invalid {} date {!r}".format(label, text)) from exc


def _parse_context(node: etree._Element) -> ContextRecord:
    context_id = node.get("id")
    if not context_id:
        raise XBRLParseError("context is missing id")
    identifier = node.find("./{}/{}".format(_x("entity"), _x("identifier")))
    if identifier is None or not (identifier.text or "").strip():
        raise XBRLParseError("context {} is missing entity identifier".format(context_id))

    period = node.find(_x("period"))
    if period is None:
        raise XBRLParseError("context {} is missing period".format(context_id))
    instant_node = period.find(_x("instant"))
    start_node = period.find(_x("startDate"))
    end_node = period.find(_x("endDate"))
    forever_node = period.find(_x("forever"))
    if instant_node is not None:
        kind = PeriodKind.INSTANT
        instant = _parse_date(instant_node.text, "instant")
        start = end = None
    elif start_node is not None and end_node is not None:
        kind = PeriodKind.DURATION
        instant = None
        start = _parse_date(start_node.text, "start")
        end = _parse_date(end_node.text, "end")
        if end < start:
            raise XBRLParseError("context {} ends before it starts".format(context_id))
    elif forever_node is not None:
        kind = PeriodKind.FOREVER
        instant = start = end = None
    else:
        raise XBRLParseError("context {} has an invalid period".format(context_id))

    dimensions = tuple(_parse_dimensions(node))
    return ContextRecord(
        context_id=context_id,
        entity_identifier=(identifier.text or "").strip(),
        entity_scheme=identifier.get("scheme"),
        period_kind=kind,
        period_start=start,
        period_end=end,
        instant=instant,
        dimensions=dimensions,
    )


def _parse_dimensions(node: etree._Element) -> Iterable[Dimension]:
    namespaces: Dict[Optional[str], str] = dict(node.nsmap)
    for member in node.findall(".//{{{}}}explicitMember".format(XBRLDI_NS)):
        dimension_ns, dimension_name = _resolve_qname(member.get("dimension"), namespaces)
        member_ns, member_name = _resolve_qname((member.text or "").strip(), namespaces)
        yield Dimension(dimension_ns, dimension_name, member_ns, member_name)
    for member in node.findall(".//{{{}}}typedMember".format(XBRLDI_NS)):
        dimension_ns, dimension_name = _resolve_qname(member.get("dimension"), namespaces)
        child = next(iter(member), None)
        member_ns = etree.QName(child).namespace or "" if child is not None else ""
        member_name = etree.QName(child).localname if child is not None else ""
        typed_value = "".join(child.itertext()).strip() if child is not None else ""
        yield Dimension(
            dimension_ns,
            dimension_name,
            member_ns,
            member_name,
            typed_value=typed_value,
        )


def _resolve_qname(
    lexical: Optional[str], namespaces: Dict[Optional[str], str]
) -> Tuple[str, str]:
    if not lexical:
        return "", ""
    if ":" not in lexical:
        return namespaces.get(None, ""), lexical
    prefix, local_name = lexical.split(":", 1)
    return namespaces.get(prefix, ""), local_name


def _parse_unit(node: etree._Element) -> UnitRecord:
    unit_id = node.get("id")
    if not unit_id:
        raise XBRLParseError("unit is missing id")
    direct_measures = tuple(_measure_text(item) for item in node.findall(_x("measure")))
    divide = node.find(_x("divide"))
    if divide is None:
        return UnitRecord(unit_id=unit_id, measures=direct_measures)
    numerator = tuple(
        _measure_text(item)
        for item in divide.findall("./{}/{}".format(_x("unitNumerator"), _x("measure")))
    )
    denominator = tuple(
        _measure_text(item)
        for item in divide.findall("./{}/{}".format(_x("unitDenominator"), _x("measure")))
    )
    return UnitRecord(
        unit_id=unit_id,
        measures=(),
        numerator_measures=numerator,
        denominator_measures=denominator,
    )


def _measure_text(node: etree._Element) -> str:
    return (node.text or "").strip()


def _decimal_or_none(value: str) -> Optional[Decimal]:
    normalized = value.strip().replace(",", "")
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None

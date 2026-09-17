"""Taxonomy-version-independent XML/XBRL fact extraction."""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Tuple

from lxml import etree

from .models import DocumentFormatError


DII_MEMBERS: Tuple[str, ...] = (
    "MutualFundsOrUTIMember",
    "AlternativeInvestmentFundsMember",
    "BanksMember",
    "InsuranceCompaniesMember",
    "ProvidentFundsOrPensionFundsMember",
    "SovereignWealthFundsDomesticMember",
    "NBFCsRegisteredWithRBIMember",
    "OtherFinancialInstitutionsMember",
)
FII_FPI_MEMBERS: Tuple[str, ...] = (
    "InstitutionsForeignPortfolioInvestorCategoryOneMember",
    "InstitutionsForeignPortfolioInvestorCategoryTwoMember",
    "OtherInstitutionsForeignMember",
)
PARENT_MEMBERS = {
    "dii_percentage": "InstitutionsDomesticMember",
    "fii_fpi_percentage": "InstitutionsForeignMember",
}


def local_name(qname: str) -> str:
    if qname.startswith("{"):
        return qname.split("}", 1)[1]
    return qname.split(":")[-1]


def namespace_name(qname: str) -> Optional[str]:
    if qname.startswith("{"):
        return qname[1:].split("}", 1)[0]
    return None


@dataclass(frozen=True)
class XbrlFact:
    qname: str
    local_name: str
    namespace: Optional[str]
    context_id: Optional[str]
    unit_id: Optional[str]
    decimals: Optional[str]
    raw_value: str
    dimensions: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class XbrlDocument:
    facts: List[XbrlFact]
    contexts: Dict[str, Dict[str, str]]
    units: Dict[str, str]


def parse_xbrl(body: bytes) -> XbrlDocument:
    """Extract facts and context dimensions by QName local name."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    try:
        root = etree.fromstring(body, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise DocumentFormatError("invalid XBRL/XML document") from exc

    contexts: Dict[str, Dict[str, str]] = {}
    units: Dict[str, str] = {}
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = local_name(element.tag)
        if name == "context":
            context_id = element.get("id")
            if not context_id:
                continue
            dimensions: Dict[str, str] = {}
            for member in element.iter():
                member_name = local_name(member.tag)
                if member_name == "explicitMember":
                    dimension = member.get("dimension", "")
                    dimensions[dimension] = (member.text or "").strip()
                elif member_name == "typedMember":
                    dimension = member.get("dimension", "")
                    child = next(iter(member), None)
                    dimensions[dimension] = (
                        "".join(child.itertext()).strip() if child is not None else ""
                    )
            contexts[context_id] = dimensions
        elif name == "unit":
            unit_id = element.get("id")
            if unit_id:
                units[unit_id] = " ".join(
                    text.strip() for text in element.itertext() if text.strip()
                )

    facts: List[XbrlFact] = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        context_id = element.get("contextRef")
        if not context_id:
            continue
        raw_value = "".join(element.itertext()).strip()
        facts.append(
            XbrlFact(
                qname=element.tag,
                local_name=local_name(element.tag),
                namespace=namespace_name(element.tag),
                context_id=context_id,
                unit_id=element.get("unitRef"),
                decimals=element.get("decimals"),
                raw_value=raw_value,
                dimensions=dict(contexts.get(context_id, {})),
            )
        )
    return XbrlDocument(facts=facts, contexts=contexts, units=units)


def _member_locals(fact: XbrlFact) -> set:
    return {local_name(value) for value in fact.dimensions.values()}


def _percentage_fact(fact: XbrlFact) -> bool:
    name = fact.local_name.lower()
    return "percent" in name and (
        "shareholding" in name or "shares" in name or "holding" in name
    )


def _decimal(fact: XbrlFact) -> Optional[Decimal]:
    try:
        return Decimal(fact.raw_value.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def _category_value(
    facts: Iterable[XbrlFact], parent: str, children: Tuple[str, ...]
) -> Tuple[Optional[Decimal], str, List[str]]:
    candidates = [fact for fact in facts if _percentage_fact(fact)]
    parent_values = [
        (fact, _decimal(fact))
        for fact in candidates
        if parent in _member_locals(fact)
    ]
    parent_values = [(fact, value) for fact, value in parent_values if value is not None]
    if parent_values:
        fact, value = parent_values[-1]
        return value, "parent", [fact.context_id or ""]

    matched: Dict[str, Tuple[XbrlFact, Decimal]] = {}
    for fact in candidates:
        value = _decimal(fact)
        if value is None:
            continue
        for member in children:
            if member in _member_locals(fact):
                matched[member] = (fact, value)
    if not matched:
        return None, "missing", []
    return (
        sum((value for _, value in matched.values()), Decimal("0")),
        "children",
        [fact.context_id or "" for fact, _ in matched.values()],
    )


def aggregate_shareholding(
    document: XbrlDocument, official_public_percentage: Optional[Decimal] = None
) -> Dict[str, object]:
    """Aggregate institutional percentages without double-counting parent/member facts.

    DII is the official ``InstitutionsDomesticMember`` fact when present, otherwise
    the eight categories in ``DII_MEMBERS``. FII/FPI is the official
    ``InstitutionsForeignMember`` fact when present, otherwise the three categories
    in ``FII_FPI_MEMBERS``. NRI, foreign-company, and foreign-national categories
    are intentionally excluded. Values at or below one are multiplied by 100 only
    when the official public percentage proves the filing uses fractional scale.
    """
    result: Dict[str, object] = {
        "official_public_percentage": official_public_percentage,
    }
    for key, children in (
        ("dii_percentage", DII_MEMBERS),
        ("fii_fpi_percentage", FII_FPI_MEMBERS),
    ):
        value, method, contexts = _category_value(
            document.facts, PARENT_MEMBERS[key], children
        )
        if (
            value is not None
            and official_public_percentage is not None
            and official_public_percentage > 1
            and abs(value) <= 1
        ):
            value *= 100
        result[key] = value
        result[key.replace("_percentage", "_aggregation")] = method
        result[key.replace("_percentage", "_context_ids")] = contexts
    return result

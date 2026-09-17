"""NSE official disclosure collectors and normalizers."""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

import requests

from .models import AcquisitionResult, DisclosureError, first, source_record
from .transport import ExchangeTransport
from .xbrl import aggregate_shareholding, parse_xbrl


NSE_BASE = "https://www.nseindia.com"
NSE_API = NSE_BASE + "/api/"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_BASE + "/companies-listing/corporate-filings-actions",
}


def _rows(payload: Any, key: Optional[str] = None) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    value = payload.get(key) if key else payload.get("data")
    if value is None and key is None and "Table" in payload:
        value = payload.get("Table")
    return [row for row in value or [] if isinstance(row, dict)]


def _params(
    start: Optional[date],
    end: Optional[date],
    symbol: Optional[str],
    index: str,
) -> Dict[str, str]:
    params = {"index": index}
    if (start is None) != (end is None):
        raise ValueError("start and end dates must be supplied together")
    if start and end:
        if start > end:
            raise ValueError("start must not be after end")
        params.update(
            from_date=start.strftime("%d-%m-%Y"),
            to_date=end.strftime("%d-%m-%Y"),
        )
    if symbol:
        params["symbol"] = symbol.upper()
    return params


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or str(value).strip() in ("", "-"):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise DisclosureError("invalid decimal value: %r" % value) from exc


class NSEClient:
    """Collect official NSE disclosure feeds without persistence side effects."""

    def __init__(
        self,
        transport: Optional[ExchangeTransport] = None,
        bootstrap: bool = True,
        **transport_options: Any
    ) -> None:
        if transport is None:
            session = requests.Session()
            session.headers.update(NSE_HEADERS)
            transport = ExchangeTransport(session=session, **transport_options)
        self.transport = transport
        if bootstrap:
            self.bootstrap()

    def bootstrap(self) -> None:
        """Warm NSE cookies; a blocked landing page does not imply API failure."""
        try:
            self.transport.session.get(NSE_BASE + "/", timeout=self.transport.timeout)
        except requests.RequestException:
            pass

    def _collect(
        self,
        endpoint: str,
        params: Dict[str, str],
        normalizer: Any,
        envelope: Optional[str] = None,
    ) -> AcquisitionResult:
        document, payload = self.transport.get(NSE_API + endpoint, params=params)
        records = [normalizer(row) for row in _rows(payload, envelope)]
        return AcquisitionResult(document=document, records=records)

    def shareholdings(
        self,
        start: date,
        end: date,
        symbol: Optional[str] = None,
        index: str = "equities",
        parse_linked_xbrl: bool = False,
    ) -> AcquisitionResult:
        result = self._collect(
            "corporate-share-holdings-master",
            _params(start, end, symbol, index),
            self._shareholding,
        )
        if parse_linked_xbrl:
            for record in result.records:
                url = record.get("xbrl_url")
                if not url:
                    continue
                linked, body = self.transport.get(urljoin(NSE_BASE, url), expected="xml")
                record["institutional"] = aggregate_shareholding(
                    parse_xbrl(body), record.get("public_percentage")
                )
                result.linked_documents.append(linked)
        return result

    @staticmethod
    def _shareholding(row: Dict[str, Any]) -> Dict[str, Any]:
        filing_id = first(row, "application_no", "appId", "id")
        revision = first(row, "revisionDate", "revisedData", "revisedStatus")
        return source_record(
            "NSE",
            "shareholding",
            row,
            {
                "symbol": first(row, "symbol"),
                "company_name": first(row, "name"),
                "isin": first(row, "isin"),
                "period": first(row, "date"),
                "promoter_percentage": _decimal(first(row, "pr_and_prgrp")),
                "public_percentage": _decimal(first(row, "public_val")),
                "employee_trust_percentage": _decimal(first(row, "employeeTrusts")),
                "submission_at": first(row, "submissionDate", "broadcastDate"),
                "xbrl_url": first(row, "xbrl"),
            },
            filing_id,
            revision,
        )

    def corporate_actions(
        self, start: date, end: date, symbol: Optional[str] = None, index: str = "equities"
    ) -> AcquisitionResult:
        return self._collect(
            "corporates-corporateActions",
            _params(start, end, symbol, index),
            self._corporate_action,
        )

    @staticmethod
    def _corporate_action(row: Dict[str, Any]) -> Dict[str, Any]:
        return source_record("NSE", "corporate_action", row, {
            "symbol": first(row, "symbol"), "company_name": first(row, "comp"),
            "purpose": first(row, "subject"), "face_value": _decimal(first(row, "faceVal")),
            "ex_date": first(row, "exDate"), "record_date": first(row, "recDate"),
            "book_closure_start": first(row, "bcStartDate"),
            "book_closure_end": first(row, "bcEndDate"), "isin": first(row, "isin"),
            "series": first(row, "series"),
        }, first(row, "appId", "id"), first(row, "revisionDate"))

    def board_meetings(
        self, start: date, end: date, symbol: Optional[str] = None, index: str = "equities"
    ) -> AcquisitionResult:
        return self._collect(
            "corporate-board-meetings", _params(start, end, symbol, index),
            self._board_meeting,
        )

    @staticmethod
    def _board_meeting(row: Dict[str, Any]) -> Dict[str, Any]:
        purpose = first(row, "bm_purpose")
        description = first(row, "bm_desc")
        details = max((value for value in (purpose, description) if value), key=len, default=None)
        return source_record("NSE", "board_meeting", row, {
            "symbol": first(row, "bm_symbol"), "company_name": first(row, "sm_name"),
            "isin": first(row, "sm_isin"), "meeting_date": first(row, "bm_date"),
            "purpose": purpose, "details": details,
            "filed_at": first(row, "bm_timestamp"), "attachment_url": first(row, "attachment"),
            "ixbrl_url": first(row, "ixbrl"),
        }, first(row, "appId", "id"), first(row, "revisionDate"))

    def pit(
        self, start: date, end: date, symbol: Optional[str] = None, index: str = "equities",
        parse_linked_xml: bool = False,
    ) -> AcquisitionResult:
        result = self._collect(
            "corporates-pit-gg", _params(start, end, symbol, index), self._pit, "data"
        )
        if parse_linked_xml:
            for record in result.records:
                url = record.get("xml_url")
                if url and not str(url).lower().endswith((".html", ".htm")):
                    linked, body = self.transport.get(urljoin(NSE_BASE, url), expected="xml")
                    record["xbrl_facts"] = [
                        fact.__dict__ for fact in parse_xbrl(body).facts
                    ]
                    result.linked_documents.append(linked)
        return result

    @staticmethod
    def _pit(row: Dict[str, Any]) -> Dict[str, Any]:
        return source_record("NSE", "pit", row, {
            "symbol": first(row, "symbol"), "company_name": first(row, "companyName"),
            "regulation": first(row, "regulation"),
            "submission_type": first(row, "typeOfSubmission"),
            "revision_remark": first(row, "revisionRemark"),
            "filed_at": first(row, "broadcastDateTime", "exchdisstime"),
            "xml_url": first(row, "xmlFileName"), "ixbrl_url": first(row, "ixbrl"),
            "previous_filing_id": first(row, "prevAppId"),
        }, first(row, "appId"), first(row, "revisionRemark", "prevAppId"))

    def sast_reg29(
        self, start: date, end: date, symbol: Optional[str] = None, index: str = "equities"
    ) -> AcquisitionResult:
        return self._collect(
            "corporate-sast-reg29", _params(start, end, symbol, index), self._sast, "data"
        )

    @staticmethod
    def _sast(row: Dict[str, Any]) -> Dict[str, Any]:
        return source_record("NSE", "sast_reg29", row, {
            "symbol": first(row, "symbol"), "company_name": first(row, "company"),
            "acquirer_name": first(row, "acquirerName"), "event_date": first(row, "acquirerDate"),
            "transaction_type": first(row, "acqSaleType", "acqType"),
            "acquisition_mode": first(row, "acquisitionMode"),
            "shares_acquired": _decimal(first(row, "noOfShareAcq")),
            "shares_sold": _decimal(first(row, "noOfShareSale")),
            "shares_after": _decimal(first(row, "noOfShareAft")),
            "percentage_after": _decimal(first(row, "totAftShare")),
            "diluted_percentage_after": _decimal(first(row, "totAftDiluted")),
            "attachment_url": first(row, "attachement"),
        }, first(row, "application_no"), first(row, "revisionDate", "timestamp"))

    def fii_dii(self) -> AcquisitionResult:
        return self._collect("fiidiiTradeReact", {}, self._fii_dii)

    @staticmethod
    def _fii_dii(row: Dict[str, Any]) -> Dict[str, Any]:
        buy = _decimal(first(row, "buyValue"))
        sell = _decimal(first(row, "sellValue"))
        net = _decimal(first(row, "netValue"))
        if buy is None or sell is None or net is None:
            raise DisclosureError("FII/DII row has missing activity values")
        if abs((buy - sell) - net) > Decimal("0.01"):
            raise DisclosureError(
                "FII/DII net value does not equal buy minus sell: %r" % row
            )
        category = first(row, "category")
        if category not in ("DII", "FII/FPI"):
            raise DisclosureError("unexpected FII/DII category: %r" % category)
        return source_record("NSE", "fii_dii", row, {
            "date": first(row, "date"), "category": category, "buy_value": buy,
            "sell_value": sell, "net_value": net, "unit": "INR crore",
        }, "%s:%s" % (first(row, "date"), category))

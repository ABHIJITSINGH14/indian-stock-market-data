"""Stable BSE JSON fallbacks keyed by numeric scrip code."""

from datetime import date
from typing import Any, Dict, List, Optional

import requests

from .models import AcquisitionResult, first, source_record
from .transport import ExchangeTransport


BSE_BASE = "https://api.bseindia.com/BseIndiaAPI/api/"
BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}


def _tables(payload: Any, names: List[str]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows: List[Dict[str, Any]] = []
    for name in names:
        value = payload.get(name)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    return rows


class BSEClient:
    """Collect BSE fallback/enrichment feeds using stable `/w` routes."""

    def __init__(
        self, transport: Optional[ExchangeTransport] = None, **transport_options: Any
    ) -> None:
        if transport is None:
            session = requests.Session()
            session.headers.update(BSE_HEADERS)
            transport = ExchangeTransport(session=session, **transport_options)
        self.transport = transport

    @staticmethod
    def _params(scripcode: int, start: date, end: date) -> Dict[str, str]:
        if not isinstance(scripcode, int) or scripcode <= 0:
            raise ValueError("scripcode must be a positive integer")
        if start > end:
            raise ValueError("start must not be after end")
        return {
            "scripcode": str(scripcode),
            "fromdt": start.strftime("%d/%m/%Y"),
            "todt": end.strftime("%d/%m/%Y"),
            "type": "0",
            "year": "",
            "Masterid": "",
            "scripcomare": "",
            "qtrid": "",
        }

    def _collect(
        self, endpoint: str, params: Dict[str, str], tables: List[str], normalizer: Any
    ) -> AcquisitionResult:
        document, payload = self.transport.get(BSE_BASE + endpoint, params=params)
        return AcquisitionResult(
            document=document,
            records=[normalizer(row) for row in _tables(payload, tables)],
        )

    def corporate_actions(
        self, scripcode: int, start: date, end: date
    ) -> AcquisitionResult:
        # Table2 is the canonical detailed stream; summary tables duplicate it.
        return self._collect(
            "CorporateAction/w", self._params(scripcode, start, end),
            ["Table2"], self._corporate_action,
        )

    @staticmethod
    def _corporate_action(row: Dict[str, Any]) -> Dict[str, Any]:
        return source_record("BSE", "corporate_action", row, {
            "scripcode": first(row, "scrip_code", "SCRIP_CODE"),
            "company_name": first(row, "sLongName", "LONG_NAME"),
            "symbol": first(row, "short_name", "Short_name"),
            "purpose": first(row, "purpose", "Details"), "ex_date": first(row, "Ex_date"),
            "record_date": first(row, "BCRD"), "payment_date": first(row, "PAYMENT_DATE"),
        }, first(row, "id", "purpose_code", "Ex_date"), first(row, "ModifiedDate"))

    def board_meetings(
        self, scripcode: int, start: date, end: date
    ) -> AcquisitionResult:
        return self._collect(
            "BoardMeeting/w", self._params(scripcode, start, end),
            ["Table"], self._board_meeting,
        )

    @staticmethod
    def _board_meeting(row: Dict[str, Any]) -> Dict[str, Any]:
        return source_record("BSE", "board_meeting", row, {
            "scripcode": first(row, "scrip_code"), "symbol": first(row, "Short_name"),
            "company_name": first(row, "LONG_NAME"), "purpose": first(row, "Purpose_name"),
            "meeting_date": first(row, "meeting_date"), "filed_at": first(row, "tm"),
        }, first(row, "id", "tm"))

    def insider_trades(
        self, scripcode: int, start: date, end: date, legacy: bool = False
    ) -> AcquisitionResult:
        endpoint = "InsiderTrade92/w" if legacy else "InsiderTrade15/w"
        normalizer = self._legacy_insider_trade if legacy else self._insider_trade
        return self._collect(
            endpoint, self._params(scripcode, start, end), ["Table"], normalizer
        )

    @staticmethod
    def _insider_trade(row: Dict[str, Any]) -> Dict[str, Any]:
        return source_record("BSE", "pit", row, {
            "scripcode": first(row, "Fld_ScripCode"), "company_name": first(row, "Companyname"),
            "person_name": first(row, "Fld_PromoterName"),
            "person_category": first(row, "Fld_PersonCatgName", "Fld_PromoterCatg"),
            "transaction_type": first(row, "Fld_TransactionType"),
            "transaction_from": first(row, "Fld_FromDate"),
            "transaction_to": first(row, "Fld_ToDate"),
            "filed_at": first(row, "Fld_DateIntimation"),
            "mode": first(row, "ModeOfAquisation", "Fld_ModeofAcquisition", "Fld_Mode"),
            "xbrl_url": first(row, "xbrlurl"),
        }, first(row, "Fld_ID"), first(row, "ModifiedDate", "Fld_DateIntimation"))

    @staticmethod
    def _legacy_insider_trade(row: Dict[str, Any]) -> Dict[str, Any]:
        return source_record("BSE", "pit_legacy", row, {
            "scripcode": first(row, "scrip_code"), "company_name": first(row, "Company_Name"),
            "person_name": first(row, "Insider_name"),
            "transaction_date": first(row, "transaction_date"),
            "transaction_type": first(row, "Buy_Sell"), "quantity": first(row, "Quantity"),
            "quantity_after": first(row, "Quantity_after"), "mode": first(row, "FLD_MODE"),
            "regulation": first(row, "Flag"),
        }, first(row, "id", "ord"), first(row, "Modified_date"))

    def sast(self, scripcode: int, start: date, end: date) -> AcquisitionResult:
        return self._collect(
            "SAST/w", self._params(scripcode, start, end), ["Table"], self._sast
        )

    @staticmethod
    def _sast(row: Dict[str, Any]) -> Dict[str, Any]:
        return source_record("BSE", "sast", row, {
            "scripcode": first(row, "scrip_code"), "company_name": first(row, "Company_Name"),
            "shareholder_name": first(row, "shareholdername"),
            "event_date": first(row, "Acquisition_date", "Fld_AcqSoldDateFrom"),
            "transaction_type": first(row, "Acq_Sale"),
            "quantity": first(row, "Acq_sale_qty"), "percentage": first(row, "Acq_sale_Pct"),
            "quantity_after": first(row, "Acquisition_After"),
            "percentage_after": first(row, "Acquisition_Pct_After"),
            "diluted_percentage_after": first(row, "Fld_TotDilPerAfterAcq"),
            "regulation": first(row, "flag"), "mode": first(row, "FLD_MODE"),
        }, first(row, "id", "ord"), first(row, "modify_date", "NEWDT"))

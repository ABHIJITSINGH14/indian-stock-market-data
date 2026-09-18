"""Official NSE and BSE equity master and bhavcopy sources."""

import csv
import io
import json
import zipfile
from datetime import date
from typing import Dict, Iterable, List, Mapping

from market_data.http import ExchangeHTTPClient, SourceResponseError
from market_data.normalization import clean_text


NSE_BASE_URL = "https://www.nseindia.com"
NSE_EQUITY_MASTER_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_BHAVCOPY_URL = (
    "https://archives.nseindia.com/content/historical/EQUITIES/"
    "{year}/{month}/cm{day}{month}{year}bhav.csv.zip"
)
NSE_UDIFF_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)
BSE_BASE_URL = "https://www.bseindia.com"
BSE_EQUITY_MASTER_URL = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
BSE_EQUITY_MASTER_FALLBACK_URL = "https://www.bseindia.com/api/meghraj/equitieslist"
BSE_BHAVCOPY_URL = "https://www.bseindia.com/download/BhavCopy/Equity/EQ{day}{month}{year2}_CSV.ZIP"
BSE_UDIFF_BHAVCOPY_URL = (
    "https://www.bseindia.com/download/BhavCopy/Equity/"
    "BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.zip"
)
BSE_UDIFF_BHAVCOPY_CSV_URL = (
    "https://www.bseindia.com/download/BhavCopy/Equity/"
    "BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.CSV"
)


def _keyed(row: Mapping[str, object]) -> Dict[str, object]:
    return {clean_text(key).upper().replace(" ", "_"): value for key, value in row.items()}


def _value(row: Mapping[str, object], *keys: str) -> object:
    keyed = _keyed(row)
    for key in keys:
        value = keyed.get(key)
        if value is not None and clean_text(value):
            return value
    return None


def _float(value: object):
    text = clean_text(value).replace(",", "")
    return float(text) if text and text not in {"-", "NA"} else None


def _int(value: object):
    number = _float(value)
    return int(number) if number is not None else None


def _csv_rows(data: bytes) -> List[Dict[str, str]]:
    text = data.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def _zip_csv_rows(data: bytes) -> List[Dict[str, str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.lower().endswith((".csv", ".txt"))
            ]
            if not names:
                raise SourceResponseError("Archive contains no CSV file")
            return _csv_rows(archive.read(names[0]))
    except zipfile.BadZipFile as exc:
        raise SourceResponseError("Invalid official ZIP archive") from exc


def parse_nse_master(data: bytes) -> List[Dict[str, object]]:
    records = []
    for row in _csv_rows(data):
        symbol = _value(row, "SYMBOL")
        if not symbol:
            continue
        records.append(
            {
                "exchange": "NSE",
                "symbol": symbol,
                "name": _value(row, "NAME_OF_COMPANY", "NAME") or symbol,
                "isin": _value(row, "ISIN_NUMBER", "ISIN"),
                "series": _value(row, "SERIES") or "",
                "source": "nse_equity_master",
                "active": True,
                "raw_data": row,
            }
        )
    if not records:
        raise SourceResponseError("NSE equity master contained no securities")
    return records


def parse_bse_master(payload: object) -> List[Dict[str, object]]:
    if isinstance(payload, dict):
        rows = (
            payload.get("Table")
            or payload.get("Table1")
            or payload.get("Data")
            or payload.get("data")
            or []
        )
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    records = []
    for row in rows:
        symbol = _value(row, "SCRIP_ID", "SCRIPID", "SCRIP_NAME", "SYMBOL")
        scrip_code = _value(row, "SCRIP_CD", "SCRIP_CODE", "SCRIPCODE", "CODE")
        if not symbol and not scrip_code:
            continue
        records.append(
            {
                "exchange": "BSE",
                "symbol": symbol or scrip_code,
                "name": _value(
                    row, "SCRIP_NAME", "SCRIPNAME", "COMPANY_NAME", "SCRIP_ID"
                )
                or symbol
                or scrip_code,
                "isin": _value(row, "ISIN_NUMBER", "ISIN_NO", "ISIN"),
                "series": _value(row, "GROUP_NAME", "GROUP", "SERIES") or "",
                "scrip_code": scrip_code or "",
                "source": "bse_equity_master",
                "active": clean_text(_value(row, "STATUS") or "Active").lower()
                not in {"inactive", "suspended"},
                "raw_data": row,
            }
        )
    if not records:
        raise SourceResponseError("BSE equity master contained no securities")
    return records


def parse_nse_bhavcopy(data: bytes, trading_date: date) -> List[Dict[str, object]]:
    records = []
    for row in _zip_csv_rows(data):
        series = clean_text(_value(row, "SERIES", "SCTYSRS")).upper()
        symbol = _value(row, "SYMBOL", "TCKRSYMB")
        close = _float(_value(row, "CLOSE", "CLSPRIC"))
        if (
            not symbol
            or close is None
            or series not in {"EQ", "BE", "BZ", "SM", "ST"}
        ):
            continue
        records.append(
            {
                "exchange": "NSE",
                "symbol": symbol,
                "name": symbol,
                "series": series,
                "trading_date": trading_date,
                "open": _float(_value(row, "OPEN", "OPNPRIC")),
                "high": _float(_value(row, "HIGH", "HGHPRIC")),
                "low": _float(_value(row, "LOW", "LWPRIC")),
                "close": close,
                "last": _float(_value(row, "LAST", "LASTPRIC")),
                "previous_close": _float(
                    _value(row, "PREVCLOSE", "PREV_CLOSE", "PRVSCLSGPRIC")
                ),
                "volume": _int(
                    _value(row, "TOTTRDQTY", "TTL_TRD_QNTY", "TTLTRADGVOL")
                ),
                "turnover": _float(
                    _value(row, "TOTTRDVAL", "TURNOVER_LACS", "TTLTRFVAL")
                ),
                "trades": _int(
                    _value(row, "TOTALTRADES", "NO_OF_TRADES", "TTLNBOFTXSEXCTD")
                ),
                "isin": _value(row, "ISIN"),
                "source": "nse_bhavcopy",
                "raw_data": row,
            }
        )
    if not records:
        raise SourceResponseError("NSE bhavcopy contained no equity rows")
    return records


def parse_bse_bhavcopy(data: bytes, trading_date: date) -> List[Dict[str, object]]:
    records = []
    rows = _zip_csv_rows(data) if data.startswith(b"PK") else _csv_rows(data)
    for row in rows:
        scrip_code = _value(row, "SC_CODE", "SCRIP_CD", "FININSTRMID")
        symbol = _value(row, "SC_NAME", "SCRIP_ID", "TCKRSYMB") or scrip_code
        close = _float(_value(row, "CLOSE", "CLSPRIC"))
        if not symbol or close is None:
            continue
        records.append(
            {
                "exchange": "BSE",
                "symbol": symbol,
                "name": symbol,
                "series": _value(row, "SC_GROUP", "GROUP", "SCTYSRS") or "",
                "scrip_code": scrip_code or "",
                "trading_date": trading_date,
                "open": _float(_value(row, "OPEN", "OPNPRIC")),
                "high": _float(_value(row, "HIGH", "HGHPRIC")),
                "low": _float(_value(row, "LOW", "LWPRIC")),
                "close": close,
                "last": _float(_value(row, "LAST", "LASTPRIC")),
                "previous_close": _float(
                    _value(row, "PREVCLOSE", "PREV_CLOSE", "PRVSCLSGPRIC")
                ),
                "volume": _int(
                    _value(row, "NO_OF_SHRS", "NO_OF_SHARES", "TTLTRADGVOL")
                ),
                "trades": _int(
                    _value(row, "NO_TRADES", "NO_OF_TRADES", "TTLNBOFTXSEXCTD")
                ),
                "turnover": _float(
                    _value(row, "NET_TURNOV", "NET_TURNOVER", "TTLTRFVAL")
                ),
                "isin": _value(row, "ISIN"),
                "source": "bse_bhavcopy",
                "raw_data": row,
            }
        )
    if not records:
        raise SourceResponseError("BSE bhavcopy contained no equity rows")
    return records


class NSESource:
    name = "nse"

    def __init__(self, client: ExchangeHTTPClient):
        self.client = client

    def fetch_master(self) -> List[Dict[str, object]]:
        response = self.client.get(
            NSE_EQUITY_MASTER_URL,
            source=self.name,
            expected="csv",
        )
        return parse_nse_master(response.content)

    def fetch_bhavcopy(self, trading_date: date) -> List[Dict[str, object]]:
        month = trading_date.strftime("%b").upper()
        urls = [
            NSE_UDIFF_BHAVCOPY_URL.format(
                yyyymmdd=trading_date.strftime("%Y%m%d")
            ),
            NSE_BHAVCOPY_URL.format(
                year=trading_date.strftime("%Y"),
                month=month,
                day=trading_date.strftime("%d"),
            ),
        ]
        errors = []
        for url in urls:
            try:
                response = self.client.get(
                    url,
                    source=self.name,
                    referer=NSE_BASE_URL,
                    expected="zip",
                )
                return parse_nse_bhavcopy(response.content, trading_date)
            except Exception as exc:
                errors.append("{}: {}".format(url, exc))
        raise SourceResponseError("; ".join(errors))


class BSESource:
    name = "bse"

    def __init__(self, client: ExchangeHTTPClient):
        self.client = client

    def fetch_master(self) -> List[Dict[str, object]]:
        params = {
            "Group": "",
            "Scripcode": "",
            "industry": "",
            "segment": "Equity",
            "status": "Active",
        }
        errors = []
        for url, request_params in (
            (BSE_EQUITY_MASTER_URL, params),
            (BSE_EQUITY_MASTER_FALLBACK_URL, None),
        ):
            try:
                response = self.client.get(
                    url,
                    source=self.name,
                    base_url=BSE_BASE_URL,
                    params=request_params,
                    expected="json",
                )
                return parse_bse_master(response.json())
            except Exception as exc:
                errors.append("{}: {}".format(url, exc))
        raise SourceResponseError("; ".join(errors))

    def fetch_bhavcopy(self, trading_date: date) -> List[Dict[str, object]]:
        urls = [
            (
                BSE_UDIFF_BHAVCOPY_CSV_URL.format(
                    yyyymmdd=trading_date.strftime("%Y%m%d")
                ),
                "csv",
            ),
            (
                BSE_UDIFF_BHAVCOPY_URL.format(
                    yyyymmdd=trading_date.strftime("%Y%m%d")
                ),
                "zip",
            ),
            (
                BSE_BHAVCOPY_URL.format(
                    day=trading_date.strftime("%d"),
                    month=trading_date.strftime("%m"),
                    year2=trading_date.strftime("%y"),
                ),
                "zip",
            ),
        ]
        errors = []
        for url, expected in urls:
            try:
                response = self.client.get(
                    url, source=self.name, base_url=BSE_BASE_URL, expected=expected
                )
                return parse_bse_bhavcopy(response.content, trading_date)
            except Exception as exc:
                errors.append("{}: {}".format(url, exc))
        raise SourceResponseError("; ".join(errors))

"""Official NSE/BSE disclosure collection into the shared SQLite database."""

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import requests
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    func,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from fundamentals import (
    ContextQuery,
    FilingIdentity,
    MetricMapper,
    StatementScope,
    parse_xbrl,
)
from market_data.database import (
    MarketDatabase,
    exchange_symbols,
    metadata as disclosure_metadata,
    securities,
)
from market_data.normalization import clean_text, normalize_symbol


filings = Table(
    "filings",
    disclosure_metadata,
    Column("id", Integer, primary_key=True),
    Column("security_id", Integer, ForeignKey("securities.id")),
    Column("symbol", String(64)),
    Column("exchange", String(3), nullable=False),
    Column("dataset", String(32), nullable=False),
    Column("external_id", String(255), nullable=False),
    Column("filing_date", Date),
    Column("period_start", Date),
    Column("period_end", Date),
    Column("document_url", Text),
    Column("document_sha256", String(64), nullable=False),
    Column("is_revision", Boolean, nullable=False, default=False),
    Column("raw_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "exchange", "dataset", "external_id", "document_sha256",
        name="uq_filing_identity",
    ),
)
Index("ix_filings_symbol_dataset_date", filings.c.symbol, filings.c.dataset, filings.c.filing_date)

raw_documents = Table(
    "raw_documents",
    disclosure_metadata,
    Column("sha256", String(64), primary_key=True),
    Column("filing_id", Integer, ForeignKey("filings.id"), nullable=False),
    Column("url", Text, nullable=False),
    Column("retrieved_at", DateTime(timezone=True), nullable=False),
    Column("content_type", String(255)),
    Column("body", LargeBinary, nullable=False),
)

shareholding_patterns = Table(
    "shareholding_patterns",
    disclosure_metadata,
    Column("filing_id", Integer, ForeignKey("filings.id"), primary_key=True),
    Column("security_id", Integer, ForeignKey("securities.id")),
    Column("symbol", String(64), nullable=False),
    Column("quarter_end", Date),
    Column("promoter_percent", Float),
    Column("fii_percent", Float),
    Column("dii_percent", Float),
    Column("public_percent", Float),
    Column("non_institution_public_percent", Float),
)

financial_facts = Table(
    "financial_facts",
    disclosure_metadata,
    Column("id", Integer, primary_key=True),
    Column("filing_id", Integer, ForeignKey("filings.id"), nullable=False),
    Column("security_id", Integer, ForeignKey("securities.id")),
    Column("symbol", String(64), nullable=False),
    Column("concept", String(255), nullable=False),
    Column("namespace", Text, nullable=False),
    Column("context_id", String(255), nullable=False),
    Column("period_start", Date),
    Column("period_end", Date),
    Column("instant", Date),
    Column("dimensions_json", Text, nullable=False),
    Column("unit", String(255)),
    Column("decimals", String(32)),
    Column("value_text", Text),
    Column("value_numeric", Float),
    UniqueConstraint(
        "filing_id", "namespace", "concept", "context_id", "unit",
        name="uq_financial_fact",
    ),
)
Index("ix_financial_facts_symbol_period", financial_facts.c.symbol, financial_facts.c.period_end)

financial_metrics = Table(
    "financial_metrics",
    disclosure_metadata,
    Column("filing_id", Integer, ForeignKey("filings.id"), primary_key=True),
    Column("symbol", String(64), primary_key=True),
    Column("filing_date", Date, primary_key=True),
    Column("period_end", Date, primary_key=True),
    Column("metric", String(64), primary_key=True),
    Column("value", Float, nullable=False),
    Column("source", String(16), nullable=False),
    Column("scope", String(16), nullable=False),
    Column("derivation", Text),
)

corporate_actions = Table(
    "corporate_actions",
    disclosure_metadata,
    Column("filing_id", Integer, ForeignKey("filings.id"), primary_key=True),
    Column("security_id", Integer, ForeignKey("securities.id")),
    Column("symbol", String(64), nullable=False),
    Column("action_type", String(32)),
    Column("subject", Text, nullable=False),
    Column("ex_date", Date),
    Column("record_date", Date),
    Column("face_value", Float),
    Column("amount", Float),
)

board_meetings = Table(
    "board_meetings",
    disclosure_metadata,
    Column("filing_id", Integer, ForeignKey("filings.id"), primary_key=True),
    Column("security_id", Integer, ForeignKey("securities.id")),
    Column("symbol", String(64), nullable=False),
    Column("meeting_date", Date),
    Column("purpose", Text),
    Column("description", Text),
    Column("attachment_url", Text),
)

pit_disclosures = Table(
    "pit_disclosures",
    disclosure_metadata,
    Column("filing_id", Integer, ForeignKey("filings.id"), primary_key=True),
    Column("security_id", Integer, ForeignKey("securities.id")),
    Column("symbol", String(64), nullable=False),
    Column("person_name", Text),
    Column("person_category", Text),
    Column("transaction_type", String(64)),
    Column("security_type", String(128)),
    Column("quantity", Float),
    Column("transaction_value", Float),
    Column("transaction_date", Date),
    Column("pre_holding", Float),
    Column("post_holding", Float),
    Column("acquisition_mode", Text),
    Column("regulation", Text),
)

sast_disclosures = Table(
    "sast_disclosures",
    disclosure_metadata,
    Column("filing_id", Integer, ForeignKey("filings.id"), primary_key=True),
    Column("security_id", Integer, ForeignKey("securities.id")),
    Column("symbol", String(64), nullable=False),
    Column("acquirer_name", Text),
    Column("regulation", String(64)),
    Column("acquisition_sale_type", String(64)),
    Column("acquisition_mode", Text),
    Column("disclosure_date", Date),
    Column("shares_acquired", Float),
    Column("shares_sold", Float),
    Column("shares_after", Float),
    Column("percent_after", Float),
)

institutional_activity = Table(
    "institutional_activity",
    disclosure_metadata,
    Column("trade_date", Date, primary_key=True),
    Column("category", String(32), primary_key=True),
    Column("source", String(16), primary_key=True),
    Column("buy_value", Float, nullable=False),
    Column("sell_value", Float, nullable=False),
    Column("net_value", Float, nullable=False),
    Column("unit", String(32), nullable=False, default="INR crore"),
    Column("raw_json", Text, nullable=False),
)

market_metrics = Table(
    "market_metrics",
    disclosure_metadata,
    Column("symbol", String(64), primary_key=True),
    Column("as_of", Date, primary_key=True),
    Column("metric", String(64), primary_key=True),
    Column("value", Float, nullable=False),
)

disclosure_errors = Table(
    "disclosure_errors",
    disclosure_metadata,
    Column("id", Integer, primary_key=True),
    Column("exchange", String(3), nullable=False),
    Column("dataset", String(32), nullable=False),
    Column("external_id", String(255)),
    Column("url", Text),
    Column("error_type", String(128), nullable=False),
    Column("message", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class DisclosureSourceError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _date(value) -> Optional[date]:
    if value in (None, "", "-"):
        return None
    text = str(value).strip()
    for pattern in ("%d-%b-%Y", "%d-%m-%Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:11], pattern).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _number(value) -> Optional[float]:
    if value in (None, "", "-", "N.A.", "N/A"):
        return None
    try:
        return float(Decimal(str(value).replace(",", "").replace("%", "").strip()))
    except (InvalidOperation, ValueError):
        return None


def _first(record: Mapping[str, object], *names: str):
    lowered = {str(key).lower(): value for key, value in record.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def _action_type(subject: str) -> str:
    lowered = subject.lower()
    for kind in ("dividend", "bonus", "split", "rights", "buyback"):
        if kind in lowered:
            return kind
    return "other"


def _url(value, base="https://www.nseindia.com") -> Optional[str]:
    text = clean_text(value)
    if not text or text.upper() in {"-", "N/A", "NA", "NULL", "NONE"}:
        return None
    resolved = urljoin(base, text)
    if urlparse(resolved).path.rstrip("/").endswith("/-"):
        return None
    return resolved


def _external_id(record: Mapping[str, object], fallback: str) -> str:
    value = _first(
        record, "recordId", "seqNumber", "application_no", "appId",
        "Fld_ID", "id", "broadcastDate", "bm_timestamp", "timestamp",
    )
    if value not in (None, ""):
        return str(value)
    encoded = json.dumps(record, sort_keys=True, default=str).encode("utf-8")
    return "{}:{}".format(fallback, hashlib.sha256(encoded).hexdigest())


class NSEDisclosureClient:
    BASE = "https://www.nseindia.com"
    ENDPOINTS = {
        "shareholding": "/api/corporate-share-holdings-master",
        "financial_results": "/api/corporates-financial-results",
        "corporate_actions": "/api/corporates-corporateActions",
        "board_meetings": "/api/corporate-board-meetings",
        "pit": "/api/corporates-pit-gg",
        "sast": "/api/corporate-sast-reg29",
        "fii_dii": "/api/fiidiiTradeReact",
    }

    def __init__(
        self, timeout: float = 30, delay: float = 0.75, retries: int = 3,
        session: Optional[requests.Session] = None,
    ):
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self.session = session or requests.Session()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "application/json, text/plain, */*",
            "Referer": self.BASE + "/",
        }
        self._bootstrapped = False

    def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        response = self.session.get(self.BASE + "/", headers=self.headers, timeout=self.timeout)
        if response.status_code >= 400 and response.status_code != 403:
            response.raise_for_status()
        self._bootstrapped = True

    def _get(self, url: str, params=None, expect_json=False) -> requests.Response:
        self.bootstrap()
        last_error = None
        for attempt in range(self.retries + 1):
            if self.delay:
                time.sleep(self.delay)
            try:
                response = self.session.get(
                    url, params=params, headers=self.headers, timeout=self.timeout
                )
                if response.status_code in (401, 403, 429) or response.status_code >= 500:
                    raise DisclosureSourceError(
                        "{} returned HTTP {}".format(url, response.status_code)
                    )
                response.raise_for_status()
                prefix = response.content[:512].lstrip().lower()
                if prefix.startswith(b"<html") or prefix.startswith(b"<!doctype html"):
                    raise DisclosureSourceError("{} returned an HTML block page".format(url))
                if expect_json:
                    response.json()
                return response
            except (requests.RequestException, ValueError, DisclosureSourceError) as exc:
                last_error = exc
                self._bootstrapped = False
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
                    self.bootstrap()
        raise DisclosureSourceError(str(last_error))

    def index(
        self, dataset: str, start: Optional[date] = None,
        end: Optional[date] = None, symbol: Optional[str] = None,
    ) -> List[Dict[str, object]]:
        params = {}
        if dataset != "fii_dii":
            if start is None or end is None:
                raise ValueError("start and end dates are required")
            params = {
                "index": "equities",
                "from_date": start.strftime("%d-%m-%Y"),
                "to_date": end.strftime("%d-%m-%Y"),
            }
            if symbol:
                params["symbol"] = normalize_symbol(symbol, "NSE")
            if dataset == "financial_results":
                params["period"] = "Quarterly"
        payload = self._get(
            self.BASE + self.ENDPOINTS[dataset], params=params, expect_json=True
        ).json()
        if isinstance(payload, dict):
            payload = payload.get("data", [])
        if not isinstance(payload, list):
            raise DisclosureSourceError("{} returned an unexpected payload".format(dataset))
        return [item for item in payload if isinstance(item, dict)]

    def document(self, url: str) -> Tuple[bytes, str]:
        response = self._get(url)
        return response.content, response.headers.get("Content-Type", "")


class BSEDisclosureClient:
    BASE = "https://api.bseindia.com/BseIndiaAPI/api"
    ENDPOINTS = {
        "corporate_actions": "CorporateAction/w",
        "board_meetings": "BoardMeeting/w",
        "pit": "InsiderTrade15/w",
        "sast": "SAST/w",
        "financial_results": "FinancialResult/w",
    }

    def __init__(
        self, timeout: float = 30, delay: float = 0.75,
        session: Optional[requests.Session] = None,
    ):
        self.timeout = timeout
        self.delay = delay
        self.session = session or requests.Session()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.bseindia.com",
            "Referer": "https://www.bseindia.com/",
        }

    def index(
        self, dataset: str, scrip_code: str,
        start: Optional[date] = None, end: Optional[date] = None,
    ) -> Dict[str, object]:
        if dataset not in self.ENDPOINTS:
            raise ValueError("Unsupported BSE dataset: {}".format(dataset))
        params = {"scripcode": scrip_code}
        if dataset == "corporate_actions":
            params.update({
                "segment": "0", "strCat": "-1", "strPrevDate": "",
                "strScrip": "", "strSearch": "P", "strToDate": "",
                "strType": "C", "report": "CORPACTALL",
            })
        elif dataset in ("pit", "sast"):
            params.update({
                "fromdt": start.strftime("%d/%m/%Y") if start else "",
                "todt": end.strftime("%d/%m/%Y") if end else "",
                "type": "0",
            })
        if self.delay:
            time.sleep(self.delay)
        response = self.session.get(
            "{}/{}".format(self.BASE, self.ENDPOINTS[dataset]),
            params=params,
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        prefix = response.content[:512].lstrip().lower()
        if prefix.startswith(b"<html") or prefix.startswith(b"<!doctype html"):
            raise DisclosureSourceError(
                "BSE {} returned an HTML block page".format(dataset)
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DisclosureSourceError(
                "BSE {} returned invalid JSON".format(dataset)
            ) from exc
        if not isinstance(payload, dict):
            raise DisclosureSourceError(
                "BSE {} returned an unexpected payload".format(dataset)
            )
        return payload


class DisclosureStore:
    def __init__(self, database: MarketDatabase):
        self.database = database

    def initialize(self) -> None:
        disclosure_metadata.create_all(self.database.engine)
        self._deduplicate_filings()

    def _deduplicate_filings(self) -> None:
        with self.database.transaction() as connection:
            groups = connection.execute(
                select(
                    filings.c.exchange,
                    filings.c.dataset,
                    filings.c.external_id,
                )
                .group_by(
                    filings.c.exchange,
                    filings.c.dataset,
                    filings.c.external_id,
                )
                .having(func.count(filings.c.id) > 1)
            ).all()
            for exchange, dataset, external_id in groups:
                ids = connection.execute(
                    select(filings.c.id)
                    .where(
                        filings.c.exchange == exchange,
                        filings.c.dataset == dataset,
                        filings.c.external_id == external_id,
                    )
                    .order_by(
                        (filings.c.document_sha256 != "").desc(),
                        filings.c.id.desc(),
                    )
                ).scalars().all()
                keep = ids[0]
                for duplicate in ids[1:]:
                    connection.execute(
                        raw_documents.update()
                        .where(raw_documents.c.filing_id == duplicate)
                        .values(filing_id=keep)
                    )
                    for table in (
                        shareholding_patterns,
                        corporate_actions,
                        board_meetings,
                        pit_disclosures,
                        sast_disclosures,
                    ):
                        if connection.execute(
                            select(table.c.filing_id).where(table.c.filing_id == keep)
                        ).first():
                            connection.execute(
                                table.delete().where(table.c.filing_id == duplicate)
                            )
                        else:
                            connection.execute(
                                table.update()
                                .where(table.c.filing_id == duplicate)
                                .values(filing_id=keep)
                            )
                    for table in (financial_facts, financial_metrics):
                        if connection.execute(
                            select(table.c.filing_id).where(table.c.filing_id == keep).limit(1)
                        ).first():
                            connection.execute(
                                table.delete().where(table.c.filing_id == duplicate)
                            )
                        else:
                            connection.execute(
                                table.update()
                                .where(table.c.filing_id == duplicate)
                                .values(filing_id=keep)
                            )
                    connection.execute(
                        filings.delete().where(filings.c.id == duplicate)
                    )

    def security(self, record: Mapping[str, object], exchange="NSE") -> Tuple[int, str]:
        raw_symbol = _first(record, "symbol", "bm_symbol", "Short_name", "scrip_code")
        symbol = normalize_symbol(str(raw_symbol or ""), exchange)
        if not symbol:
            raise ValueError("Disclosure has no symbol")
        payload = {
            "exchange": exchange,
            "symbol": symbol,
            "series": clean_text(_first(record, "series")),
            "scrip_code": clean_text(_first(record, "scrip_code", "Fld_ScripCode")),
            "isin": clean_text(_first(record, "isin", "sm_isin")) or None,
            "name": clean_text(
                _first(record, "companyName", "company", "comp", "name", "sm_name", "LONG_NAME")
            ) or symbol,
            "source": "{}_disclosures".format(exchange.lower()),
            "raw_data": dict(record),
        }
        with self.database.transaction() as connection:
            security_id = self.database._resolve_security(connection, payload)
        return security_id, symbol

    def filing(
        self, record: Mapping[str, object], dataset: str, exchange="NSE",
        document_url: Optional[str] = None, document_sha256: str = "",
    ) -> Tuple[int, int, str]:
        security_id, symbol = self.security(record, exchange)
        filing_day = _date(
            _first(
                record, "filingDate", "submissionDate", "broadcastDate",
                "broadcastDateTime", "bm_timestamp", "timestamp", "Fld_StampDate",
            )
        )
        period_start = _date(_first(record, "fromDate"))
        period_end = _date(_first(record, "toDate", "date"))
        values = {
            "security_id": security_id,
            "symbol": symbol,
            "exchange": exchange,
            "dataset": dataset,
            "external_id": _external_id(record, dataset),
            "filing_date": filing_day,
            "period_start": period_start,
            "period_end": period_end,
            "document_url": document_url,
            "document_sha256": document_sha256,
            "is_revision": bool(_first(record, "revisedData", "prevAppId")),
            "raw_json": json.dumps(record, sort_keys=True, default=str),
            "created_at": _utcnow(),
        }
        with self.database.transaction() as connection:
            filing_id = connection.execute(
                select(filings.c.id).where(
                    filings.c.exchange == exchange,
                    filings.c.dataset == dataset,
                    filings.c.external_id == values["external_id"],
                ).order_by(filings.c.id.desc()).limit(1)
            ).scalar_one_or_none()
            if filing_id is None:
                result = connection.execute(filings.insert().values(**values))
                filing_id = result.inserted_primary_key[0]
            else:
                connection.execute(
                    filings.update().where(filings.c.id == filing_id).values(
                        security_id=security_id,
                        symbol=symbol,
                        filing_date=values["filing_date"],
                        period_start=values["period_start"],
                        period_end=values["period_end"],
                        document_url=document_url or filings.c.document_url,
                        document_sha256=document_sha256 or filings.c.document_sha256,
                        is_revision=values["is_revision"],
                        raw_json=values["raw_json"],
                    )
                )
        return int(filing_id), security_id, symbol

    def document(
        self, filing_id: int, url: str, body: bytes, content_type: str
    ) -> str:
        digest = hashlib.sha256(body).hexdigest()
        statement = sqlite_insert(raw_documents).values(
            sha256=digest,
            filing_id=filing_id,
            url=url,
            retrieved_at=_utcnow(),
            content_type=content_type,
            body=body,
        )
        with self.database.transaction() as connection:
            connection.execute(statement.on_conflict_do_nothing(index_elements=[raw_documents.c.sha256]))
        return digest

    def error(
        self, exchange: str, dataset: str, record: Mapping[str, object],
        error: Exception, url: Optional[str] = None,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                disclosure_errors.insert().values(
                    exchange=exchange,
                    dataset=dataset,
                    external_id=_external_id(record, dataset),
                    url=url,
                    error_type=type(error).__name__,
                    message=str(error),
                    created_at=_utcnow(),
                )
            )

    def upsert(self, table: Table, values: Mapping[str, object], keys: Sequence) -> None:
        statement = sqlite_insert(table).values(**values)
        key_names = {column.name for column in keys}
        updates = {
            column.name: getattr(statement.excluded, column.name)
            for column in table.c
            if column.name not in key_names
        }
        with self.database.transaction() as connection:
            connection.execute(
                statement.on_conflict_do_update(index_elements=list(keys), set_=updates)
            )


class NSEDisclosureCollector:
    DATASETS = (
        "shareholding", "financial_results", "corporate_actions",
        "board_meetings", "pit", "sast", "fii_dii",
    )

    def __init__(
        self, database: MarketDatabase, client: Optional[NSEDisclosureClient] = None,
        fetch_documents: bool = True,
    ):
        self.store = DisclosureStore(database)
        self.client = client or NSEDisclosureClient()
        self.fetch_documents = fetch_documents
        self.mapper = MetricMapper()

    def collect(
        self, start: date, end: date, datasets: Sequence[str] = DATASETS,
        symbol: Optional[str] = None, window_days: int = 30,
    ) -> Dict[str, int]:
        self.store.initialize()
        counts = {}
        for dataset in datasets:
            if dataset not in self.DATASETS:
                raise ValueError("Unsupported disclosure dataset: {}".format(dataset))
            if dataset == "fii_dii":
                rows = self.client.index(dataset)
                counts[dataset] = self._activity(rows)
                continue
            count = 0
            failed = 0
            for window_start, window_end in _windows(start, end, window_days):
                rows = self.client.index(dataset, window_start, window_end, symbol)
                for row in rows:
                    try:
                        self._record(dataset, row)
                        count += 1
                    except Exception as exc:
                        self.store.error(
                            "NSE", dataset, row, exc, _url(
                                _first(
                                    row, "xbrl", "attachment", "ixbrl",
                                    "xmlFileName", "attachement",
                                )
                            )
                        )
                        failed += 1
            counts[dataset] = count
            if failed:
                counts[dataset + "_failed"] = failed
        self.refresh_market_metrics()
        return counts

    def _record(self, dataset: str, row: Mapping[str, object]) -> None:
        if dataset == "shareholding":
            self._shareholding(row)
        elif dataset == "financial_results":
            self._financial_result(row)
        elif dataset == "corporate_actions":
            self._corporate_action(row)
        elif dataset == "board_meetings":
            self._board_meeting(row)
        elif dataset == "pit":
            self._pit(row)
        elif dataset == "sast":
            self._sast(row)

    def _document_filing(
        self, row: Mapping[str, object], dataset: str, url: Optional[str]
    ) -> Tuple[int, int, str, Optional[bytes]]:
        filing_id, security_id, symbol = self.store.filing(
            row, dataset, document_url=url
        )
        body = None
        if url and self.fetch_documents:
            body, content_type = self.client.document(url)
            digest = hashlib.sha256(body).hexdigest()
            filing_id, security_id, symbol = self.store.filing(
                row, dataset, document_url=url, document_sha256=digest
            )
            self.store.document(filing_id, url, body, content_type)
        return filing_id, security_id, symbol, body

    def _shareholding(self, row: Mapping[str, object]) -> None:
        url = _url(_first(row, "xbrl"))
        filing_id, security_id, symbol, body = self._document_filing(
            row, "shareholding", url
        )
        fii = dii = None
        if body:
            fii, dii = shareholding_institutional_split(body)
        public = _number(_first(row, "public_val"))
        values = {
            "filing_id": filing_id,
            "security_id": security_id,
            "symbol": symbol,
            "quarter_end": _date(_first(row, "date")),
            "promoter_percent": _number(_first(row, "pr_and_prgrp")),
            "fii_percent": fii,
            "dii_percent": dii,
            "public_percent": public,
            "non_institution_public_percent": (
                max(0.0, public - (fii or 0.0) - (dii or 0.0))
                if public is not None and fii is not None and dii is not None
                else None
            ),
        }
        self.store.upsert(shareholding_patterns, values, [shareholding_patterns.c.filing_id])

    def _financial_result(self, row: Mapping[str, object]) -> None:
        url = _url(_first(row, "xbrl"))
        filing_id, security_id, symbol, body = self._document_filing(
            row, "financial_results", url
        )
        if not body:
            return
        filing_date = _date(_first(row, "filingDate", "broadCastDate")) or date.today()
        identity = FilingIdentity(
            symbol=symbol,
            filing_date=filing_date,
            source_url=url or "",
            company_name=clean_text(_first(row, "companyName")) or None,
            isin=clean_text(_first(row, "isin")) or None,
            industry=clean_text(_first(row, "industry")) or None,
            financial_year=clean_text(_first(row, "financialYear")) or None,
            relating_to=clean_text(_first(row, "relatingTo")) or None,
            sequence_number=clean_text(_first(row, "seqNumber")) or None,
            consolidated=_consolidated(_first(row, "consolidated")),
            cumulative=_boolean(_first(row, "cumulative")),
            audited=_boolean(_first(row, "audited")),
        )
        parsed = parse_xbrl(body, identity)
        with self.store.database.transaction() as connection:
            connection.execute(
                financial_facts.delete().where(
                    financial_facts.c.filing_id == filing_id
                )
            )
            connection.execute(
                financial_metrics.delete().where(
                    financial_metrics.c.filing_id == filing_id
                )
            )
        fact_values = []
        for fact in parsed.facts:
            dimensions = {
                item.dimension_name: item.member_name or item.typed_value
                for item in fact.context.dimensions
            }
            fact_values.append(
                {
                    "filing_id": filing_id,
                    "security_id": security_id,
                    "symbol": symbol,
                    "concept": fact.local_name,
                    "namespace": fact.namespace,
                    "context_id": fact.context.context_id,
                    "period_start": fact.context.period_start,
                    "period_end": fact.context.period_end,
                    "instant": fact.context.instant,
                    "dimensions_json": json.dumps(dimensions, sort_keys=True),
                    "unit": fact.unit_ref or "",
                    "decimals": fact.decimals,
                    "value_text": fact.value,
                    "value_numeric": float(fact.numeric_value)
                    if fact.numeric_value is not None
                    else None,
                }
            )
        if fact_values:
            statement = sqlite_insert(financial_facts).values(fact_values)
            with self.store.database.transaction() as connection:
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            financial_facts.c.filing_id,
                            financial_facts.c.namespace,
                            financial_facts.c.concept,
                            financial_facts.c.context_id,
                            financial_facts.c.unit,
                        ],
                        set_={
                            "value_text": statement.excluded.value_text,
                            "value_numeric": statement.excluded.value_numeric,
                            "decimals": statement.excluded.decimals,
                        },
                    )
                )
        scope = (
            StatementScope.CONSOLIDATED
            if identity.consolidated
            else StatementScope.STANDALONE
            if identity.consolidated is False
            else StatementScope.UNKNOWN
        )
        for period in ("quarter", "instant"):
            for metric in self.mapper.map_filing(
                parsed, ContextQuery(period=period, scope=scope)
            ):
                period_end = metric.context.accounting_date
                if period_end is None:
                    continue
                self.store.upsert(
                    financial_metrics,
                    {
                        "filing_id": filing_id,
                        "symbol": symbol,
                        "filing_date": filing_date,
                        "period_end": period_end,
                        "metric": metric.name,
                        "value": float(metric.value),
                        "source": metric.source.value,
                        "scope": scope.value,
                        "derivation": metric.derivation,
                    },
                    [
                        financial_metrics.c.filing_id,
                        financial_metrics.c.symbol,
                        financial_metrics.c.filing_date,
                        financial_metrics.c.period_end,
                        financial_metrics.c.metric,
                    ],
                )

    def _corporate_action(self, row: Mapping[str, object]) -> None:
        filing_id, security_id, symbol, _ = self._document_filing(
            row, "corporate_actions", None
        )
        subject = clean_text(_first(row, "subject")) or "Unspecified"
        amount_match = re.search(r"(?:Rs\\.?|Re\\.?)\\s*([0-9]+(?:\\.[0-9]+)?)", subject, re.I)
        self.store.upsert(
            corporate_actions,
            {
                "filing_id": filing_id,
                "security_id": security_id,
                "symbol": symbol,
                "action_type": _action_type(subject),
                "subject": subject,
                "ex_date": _date(_first(row, "exDate")),
                "record_date": _date(_first(row, "recDate")),
                "face_value": _number(_first(row, "faceVal")),
                "amount": float(amount_match.group(1)) if amount_match else None,
            },
            [corporate_actions.c.filing_id],
        )

    def _board_meeting(self, row: Mapping[str, object]) -> None:
        url = _url(_first(row, "attachment", "ixbrl"))
        filing_id, security_id, symbol, _ = self._document_filing(
            row, "board_meetings", url
        )
        self.store.upsert(
            board_meetings,
            {
                "filing_id": filing_id,
                "security_id": security_id,
                "symbol": symbol,
                "meeting_date": _date(_first(row, "bm_date", "proposedMeetingDate")),
                "purpose": clean_text(_first(row, "bm_purpose", "meetingType")) or None,
                "description": clean_text(_first(row, "bm_desc")) or None,
                "attachment_url": url,
            },
            [board_meetings.c.filing_id],
        )

    def _pit(self, row: Mapping[str, object]) -> None:
        url = _url(_first(row, "xmlFileName", "ixbrl"))
        filing_id, security_id, symbol, _ = self._document_filing(row, "pit", url)
        self.store.upsert(
            pit_disclosures,
            {
                "filing_id": filing_id,
                "security_id": security_id,
                "symbol": symbol,
                "person_name": clean_text(_first(row, "personName", "acqName")) or None,
                "person_category": clean_text(_first(row, "categoryOfPerson")) or None,
                "transaction_type": clean_text(_first(row, "transactionType")) or None,
                "security_type": clean_text(_first(row, "securityType")) or None,
                "quantity": _number(_first(row, "quantity")),
                "transaction_value": _number(_first(row, "value")),
                "transaction_date": _date(_first(row, "transactionDate")),
                "pre_holding": _number(_first(row, "preHolding")),
                "post_holding": _number(_first(row, "postHolding")),
                "acquisition_mode": clean_text(_first(row, "acquisitionMode")) or None,
                "regulation": clean_text(_first(row, "regulation")) or None,
            },
            [pit_disclosures.c.filing_id],
        )

    def _sast(self, row: Mapping[str, object]) -> None:
        url = _url(_first(row, "attachement"))
        filing_id, security_id, symbol, _ = self._document_filing(row, "sast", url)
        self.store.upsert(
            sast_disclosures,
            {
                "filing_id": filing_id,
                "security_id": security_id,
                "symbol": symbol,
                "acquirer_name": clean_text(_first(row, "acquirerName")) or None,
                "regulation": clean_text(_first(row, "regType")) or None,
                "acquisition_sale_type": clean_text(_first(row, "acqSaleType", "acqType")) or None,
                "acquisition_mode": clean_text(_first(row, "acquisitionMode")) or None,
                "disclosure_date": _date(_first(row, "acquirerDate", "timestamp")),
                "shares_acquired": _number(_first(row, "noOfShareAcq")),
                "shares_sold": _number(_first(row, "noOfShareSale")),
                "shares_after": _number(_first(row, "noOfShareAft")),
                "percent_after": _number(_first(row, "totAftShare")),
            },
            [sast_disclosures.c.filing_id],
        )

    def _activity(self, rows: Iterable[Mapping[str, object]]) -> int:
        count = 0
        for row in rows:
            buy = _number(_first(row, "buyValue"))
            sell = _number(_first(row, "sellValue"))
            net = _number(_first(row, "netValue"))
            if buy is None or sell is None or net is None:
                raise ValueError("FII/DII row has non-numeric values")
            if abs((buy - sell) - net) > 0.02:
                raise ValueError("FII/DII net value does not match buy minus sell")
            self.store.upsert(
                institutional_activity,
                {
                    "trade_date": _date(_first(row, "date")),
                    "category": clean_text(_first(row, "category")).upper(),
                    "source": "NSE",
                    "buy_value": buy,
                    "sell_value": sell,
                    "net_value": net,
                    "unit": "INR crore",
                    "raw_json": json.dumps(row, sort_keys=True, default=str),
                },
                [
                    institutional_activity.c.trade_date,
                    institutional_activity.c.category,
                    institutional_activity.c.source,
                ],
            )
            count += 1
        return count

    def refresh_market_metrics(self) -> None:
        sql = """
        WITH priced AS (
          SELECT es.exchange_symbol AS symbol, p.trading_date AS as_of, p.close,
                 MAX(p.high) OVER (
                   PARTITION BY p.security_id ORDER BY julianday(p.trading_date)
                   RANGE BETWEEN 365 PRECEDING AND CURRENT ROW
                 ) AS high_52w,
                 MIN(p.low) OVER (
                   PARTITION BY p.security_id ORDER BY julianday(p.trading_date)
                   RANGE BETWEEN 365 PRECEDING AND CURRENT ROW
                 ) AS low_52w
          FROM daily_prices p
          JOIN exchange_symbols es ON es.security_id=p.security_id
             AND es.exchange='NSE'
          WHERE p.exchange='NSE'
        )
        SELECT symbol, as_of, close, high_52w, low_52w FROM priced
        """
        with self.store.database.transaction() as connection:
            rows = connection.exec_driver_sql(sql).mappings().all()
            values = []
            for row in rows:
                for metric in ("close", "high_52w", "low_52w"):
                    if row[metric] is not None:
                        values.append(
                            {
                                "symbol": row["symbol"],
                                "as_of": _date(row["as_of"]),
                                "metric": "price" if metric == "close" else metric,
                                "value": row[metric],
                            }
                        )
            if values:
                statement = sqlite_insert(market_metrics).values(values)
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            market_metrics.c.symbol,
                            market_metrics.c.as_of,
                            market_metrics.c.metric,
                        ],
                        set_={"value": statement.excluded.value},
                    )
                )


class BSEDisclosureCollector:
    """Per-scrip BSE enrichment for the official endpoints that remain stable."""

    DATASETS = ("corporate_actions", "board_meetings", "pit", "sast", "financial_results")

    def __init__(
        self, database: MarketDatabase, client: Optional[BSEDisclosureClient] = None,
    ):
        self.database = database
        self.store = DisclosureStore(database)
        self.client = client or BSEDisclosureClient()

    def collect(
        self, datasets: Sequence[str] = DATASETS,
        scrip_codes: Optional[Sequence[str]] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> Dict[str, int]:
        if (start is None) != (end is None):
            raise ValueError("start and end dates must be supplied together")
        if start and end and start > end:
            raise ValueError("start must not be after end")
        self.store.initialize()
        codes = list(scrip_codes or self._scrip_codes())
        counts = {dataset: 0 for dataset in datasets}
        for code in codes:
            for dataset in datasets:
                try:
                    payload = self.client.index(dataset, code, start, end)
                except Exception as exc:
                    self.store.error(
                        "BSE", dataset, {"scrip_code": code}, exc
                    )
                    counts[dataset + "_failed"] = (
                        counts.get(dataset + "_failed", 0) + 1
                    )
                    continue
                rows = self._rows(dataset, payload)
                for row in rows:
                    row = dict(row)
                    row.setdefault("scrip_code", code)
                    observed = self._record_date(dataset, row)
                    if observed and start and observed < start:
                        continue
                    if observed and end and observed > end:
                        continue
                    try:
                        self._record(dataset, row)
                        counts[dataset] += 1
                    except Exception as exc:
                        self.store.error("BSE", dataset, row, exc)
                        counts[dataset + "_failed"] = (
                            counts.get(dataset + "_failed", 0) + 1
                        )
        return counts

    @staticmethod
    def _record_date(dataset: str, row: Mapping[str, object]) -> Optional[date]:
        if dataset == "corporate_actions":
            return _date(_first(row, "Ex_date", "BCRD", "BCRD_FROM"))
        if dataset == "board_meetings":
            return _date(_first(row, "meeting_date", "tm"))
        if dataset == "pit":
            return _date(_first(row, "Fld_DateIntimation", "Fld_FromDate"))
        if dataset == "sast":
            return _date(
                _first(row, "Acquisition_date", "Fld_AcqSoldDateFrom", "NEWDT")
            )
        return None

    def _scrip_codes(self) -> List[str]:
        with self.database.engine.connect() as connection:
            return [
                str(value)
                for value in connection.execute(
                    select(exchange_symbols.c.scrip_code).where(
                        exchange_symbols.c.exchange == "BSE",
                        exchange_symbols.c.scrip_code != "",
                    )
                ).scalars()
            ]

    @staticmethod
    def _rows(dataset: str, payload: Mapping[str, object]) -> List[Mapping[str, object]]:
        if dataset == "corporate_actions":
            values = payload.get("Table2") or payload.get("Table1") or payload.get("Table") or []
        elif dataset == "financial_results":
            html = payload.get("Data")
            values = [{"html_index": html}] if html else []
        else:
            values = payload.get("Table") or []
        return [item for item in values if isinstance(item, dict)]

    def _record(self, dataset: str, row: Mapping[str, object]) -> None:
        filing_id, security_id, symbol = self.store.filing(
            row, dataset, exchange="BSE"
        )
        if dataset == "corporate_actions":
            subject = clean_text(
                _first(row, "purpose", "purpose_name", "Details", "XTYPE")
            ) or "Unspecified"
            self.store.upsert(
                corporate_actions,
                {
                    "filing_id": filing_id,
                    "security_id": security_id,
                    "symbol": symbol,
                    "action_type": _action_type(subject),
                    "subject": subject,
                    "ex_date": _date(_first(row, "Ex_date", "BCRD_FROM")),
                    "record_date": _date(_first(row, "BCRD", "BCRD_from")),
                    "face_value": _number(_first(row, "VALUE")),
                    "amount": _number(_first(row, "Amount")),
                },
                [corporate_actions.c.filing_id],
            )
        elif dataset == "board_meetings":
            self.store.upsert(
                board_meetings,
                {
                    "filing_id": filing_id,
                    "security_id": security_id,
                    "symbol": symbol,
                    "meeting_date": _date(_first(row, "meeting_date", "tm")),
                    "purpose": clean_text(_first(row, "Purpose_name")) or None,
                    "description": None,
                    "attachment_url": None,
                },
                [board_meetings.c.filing_id],
            )
        elif dataset == "pit":
            self.store.upsert(
                pit_disclosures,
                {
                    "filing_id": filing_id,
                    "security_id": security_id,
                    "symbol": symbol,
                    "person_name": clean_text(_first(row, "Fld_PromoterName")) or None,
                    "person_category": clean_text(
                        _first(row, "Fld_PersonCatgName", "Fld_PromoterCatg")
                    ) or None,
                    "transaction_type": clean_text(_first(row, "Fld_TransactionType")) or None,
                    "security_type": clean_text(_first(row, "Fld_SecurityTypeName")) or None,
                    "quantity": _number(_first(row, "Fld_SecurityNo")),
                    "transaction_value": _number(_first(row, "Fld_SecurityValue")),
                    "transaction_date": _date(_first(row, "Fld_FromDate", "Fld_ToDate")),
                    "pre_holding": _number(_first(row, "Fld_SecurityNoPrior")),
                    "post_holding": _number(_first(row, "Fld_SecurityNoPost")),
                    "acquisition_mode": clean_text(_first(row, "ModeOfAquisation")) or None,
                    "regulation": "PIT",
                },
                [pit_disclosures.c.filing_id],
            )
        elif dataset == "sast":
            self.store.upsert(
                sast_disclosures,
                {
                    "filing_id": filing_id,
                    "security_id": security_id,
                    "symbol": symbol,
                    "acquirer_name": clean_text(_first(row, "shareholdername")) or None,
                    "regulation": clean_text(_first(row, "flag")) or None,
                    "acquisition_sale_type": clean_text(_first(row, "Acq_Sale")) or None,
                    "acquisition_mode": clean_text(_first(row, "FLD_MODE")) or None,
                    "disclosure_date": _date(
                        _first(row, "Acquisition_date", "Fld_AcqSoldDateFrom", "NEWDT")
                    ),
                    "shares_acquired": (
                        _number(_first(row, "Acq_sale_qty"))
                        if "acq" in clean_text(_first(row, "Acq_Sale")).lower()
                        else None
                    ),
                    "shares_sold": (
                        _number(_first(row, "Acq_sale_qty"))
                        if "sale" in clean_text(_first(row, "Acq_Sale")).lower()
                        else None
                    ),
                    "shares_after": _number(_first(row, "Acquisition_After")),
                    "percent_after": _number(_first(row, "Acquisition_Pct_After")),
                },
                [sast_disclosures.c.filing_id],
            )
        # BSE FinancialResult/w exposes an HTML document index. The complete raw
        # index is retained in filings; structured facts come from NSE XBRL.


def shareholding_institutional_split(xml: bytes) -> Tuple[Optional[float], Optional[float]]:
    identity = FilingIdentity("UNKNOWN", date.today(), "memory://shareholding")
    parsed = parse_xbrl(xml, identity)
    fii_members = (
        "foreignportfolio", "foreigninstitution", "foreignventure",
        "foreigncorporate", "qualifiedforeign",
    )
    dii_members = (
        "mutualfund", "insurance", "bank", "financialinstitution",
        "pensionfund", "alternativeinvestmentfund",
    )
    percentages: Dict[str, float] = {}
    for fact in parsed.facts:
        concept = re.sub(r"[^a-z]", "", fact.local_name.lower())
        if "percentage" not in concept or fact.numeric_value is None:
            continue
        members = " ".join(
            re.sub(r"[^a-z]", "", item.member_name.lower())
            for item in fact.context.dimensions
        )
        value = float(fact.numeric_value)
        if abs(value) <= 1:
            value *= 100
        for group, needles in (("fii", fii_members), ("dii", dii_members)):
            if any(needle in members for needle in needles):
                percentages["{}:{}".format(group, members)] = max(
                    value, percentages.get("{}:{}".format(group, members), 0.0)
                )
    fii = sum(v for key, v in percentages.items() if key.startswith("fii:"))
    dii = sum(v for key, v in percentages.items() if key.startswith("dii:"))
    return (fii or None), (dii or None)


def _windows(start: date, end: date, days: int) -> Iterator[Tuple[date, date]]:
    if start > end:
        raise ValueError("start must not be after end")
    current = start
    while current <= end:
        window_end = min(current + timedelta(days=days - 1), end)
        yield current, window_end
        current = window_end + timedelta(days=1)


def _boolean(value) -> Optional[bool]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("yes", "true", "1", "y", "audited", "consolidated"):
        return True
    if text in ("no", "false", "0", "n", "unaudited", "standalone"):
        return False
    return None


def _consolidated(value) -> Optional[bool]:
    text = clean_text(value).lower()
    if "consolidated" in text:
        return True
    if "standalone" in text:
        return False
    return _boolean(value)

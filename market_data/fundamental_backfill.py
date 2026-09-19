"""Resumable bulk backfill and coverage reporting for official XBRL filings."""

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple
from urllib.parse import urljoin

from sqlalchemy import String, case, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from fundamentals.metrics import DEFAULT_ALIASES
from market_data.disclosures import (
    BSEDisclosureClient,
    DisclosureStore,
    NSEDisclosureClient,
    NSEDisclosureCollector,
    _date,
    _first,
    _url,
    _windows,
    bse_financial_checkpoints,
    filing_document_status,
    filing_index_checkpoints,
    filings,
    financial_fact_instances,
    financial_metrics,
    raw_documents,
    shareholding_patterns,
)
from market_data.database import exchange_symbols


BACKFILL_DATASETS = ("financial_results", "shareholding")
BSE_XBRL_BASE = "https://www.bseindia.com/XBRLFILES/"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _document_failure_status(error: Exception) -> str:
    return "unavailable" if "404 Client Error" in str(error) else "failed"


class FundamentalBackfill:
    """Fetch bulk filing indexes sequentially and linked documents concurrently."""

    def __init__(
        self,
        database,
        index_client: NSEDisclosureClient,
        document_client_factory: Optional[Callable[[], NSEDisclosureClient]] = None,
        workers: int = 4,
        download_interval: float = 0.75,
        output: Optional[TextIO] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if workers < 1 or workers > 16:
            raise ValueError("workers must be between 1 and 16")
        self.database = database
        self.index_client = index_client
        self.document_client_factory = document_client_factory or (
            lambda: index_client
        )
        self.workers = workers
        self.download_interval = download_interval
        self.output = output
        self.clock = clock
        self.store = DisclosureStore(database)
        self.collector = NSEDisclosureCollector(
            database, client=index_client, fetch_documents=False
        )
        self._worker_local = threading.local()
        self._worker_clients: List[NSEDisclosureClient] = []
        self._worker_clients_lock = threading.Lock()
        self._download_lock = threading.Lock()
        self._last_download = 0.0

    def run(
        self,
        start: date,
        end: date,
        datasets: Sequence[str] = BACKFILL_DATASETS,
        window_days: int = 30,
        resume: bool = True,
        retry_failed: bool = False,
        refresh_documents: bool = False,
    ) -> Dict[str, object]:
        if start > end:
            raise ValueError("start must not be after end")
        if window_days < 1 or window_days > 90:
            raise ValueError("window_days must be between 1 and 90")
        invalid = sorted(set(datasets) - set(BACKFILL_DATASETS))
        if invalid:
            raise ValueError("Unsupported backfill datasets: {}".format(", ".join(invalid)))

        self.store.initialize()
        windows = [
            (dataset, window_start, window_end)
            for dataset in datasets
            for window_start, window_end in _windows(start, end, window_days)
        ]
        summary: Dict[str, object] = {
            "windows_total": len(windows),
            "windows_completed": 0,
            "windows_failed": 0,
            "windows_skipped": 0,
            "filings": 0,
            "documents": 0,
            "documents_cached": 0,
            "documents_failed": 0,
            "documents_missing": 0,
            "documents_unavailable": 0,
            "interrupted": False,
        }
        started = self.clock()
        try:
            for position, (dataset, window_start, window_end) in enumerate(windows, 1):
                status = self._checkpoint_status(dataset, window_start, window_end)
                if resume and status == "complete" and not refresh_documents:
                    summary["windows_skipped"] += 1
                    self._progress(position, windows, started, dataset, window_start, window_end)
                    continue
                if retry_failed and status not in {
                    "failed", "partial", "interrupted", None,
                }:
                    summary["windows_skipped"] += 1
                    continue
                self._set_checkpoint(
                    dataset, window_start, window_end, "running", increment_attempt=True
                )
                try:
                    rows = self.index_client.index(
                        dataset, window_start, window_end, symbol=None
                    )
                    result = self._process_window(
                        dataset, rows, refresh_documents=refresh_documents
                    )
                    for key in (
                        "filings",
                        "documents",
                        "documents_cached",
                        "documents_failed",
                        "documents_missing",
                        "documents_unavailable",
                    ):
                        summary[key] += result[key]
                    final_status = "partial" if result["documents_failed"] else "complete"
                    self._set_checkpoint(
                        dataset,
                        window_start,
                        window_end,
                        final_status,
                        row_count=len(rows),
                        processed_count=result["filings"],
                        failed_count=result["documents_failed"],
                    )
                    if final_status == "complete":
                        summary["windows_completed"] += 1
                    else:
                        summary["windows_failed"] += 1
                except KeyboardInterrupt:
                    self._set_checkpoint(
                        dataset, window_start, window_end, "interrupted",
                        error="Interrupted by user",
                    )
                    summary["interrupted"] = True
                    raise
                except Exception as exc:
                    self._set_checkpoint(
                        dataset, window_start, window_end, "failed", error=str(exc)
                    )
                    summary["windows_failed"] += 1
                self._progress(position, windows, started, dataset, window_start, window_end)
        except KeyboardInterrupt:
            summary["interrupted"] = True
        finally:
            self._close_worker_clients()
        summary["elapsed_seconds"] = round(self.clock() - started, 3)
        return summary

    def _process_window(
        self,
        dataset: str,
        rows: Sequence[Mapping[str, object]],
        refresh_documents: bool,
    ) -> Dict[str, int]:
        result = {
            "filings": 0,
            "documents": 0,
            "documents_cached": 0,
            "documents_failed": 0,
            "documents_missing": 0,
            "documents_unavailable": 0,
        }
        downloads: Dict[Future, Tuple[Mapping[str, object], str]] = {}
        with ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="xbrl-download"
        ) as executor:
            for row in rows:
                url = _url(_first(row, "xbrl"))
                self.store.filing(row, dataset, document_url=url)
                result["filings"] += 1
                if not url:
                    self.collector.record_with_document(dataset, row, None, url=None)
                    result["documents_missing"] += 1
                    continue
                cached = None if refresh_documents else self.store.cached_document(
                    row, dataset, url
                )
                if (
                    cached is None
                    and not refresh_documents
                    and self.store.document_status(dataset, row, url) == "unavailable"
                ):
                    result["documents_unavailable"] += 1
                    continue
                if cached is not None:
                    body, content_type, digest = cached
                    try:
                        self.collector.record_with_document(
                            dataset, row, body, content_type, url
                        )
                        self.store.set_document_status(
                            dataset, row, url, "complete", digest=digest
                        )
                        result["documents_cached"] += 1
                    except Exception as exc:
                        status = self._document_failure(dataset, row, url, exc)
                        result[
                            "documents_unavailable"
                            if status == "unavailable"
                            else "documents_failed"
                        ] += 1
                    continue
                self.store.set_document_status(
                    dataset, row, url, "pending", increment_attempt=True
                )
                future = executor.submit(self._download, url)
                downloads[future] = (row, url)

            try:
                for future in as_completed(downloads):
                    row, url = downloads[future]
                    try:
                        body, content_type = future.result()
                        self.collector.record_with_document(
                            dataset, row, body, content_type, url
                        )
                        digest = self.store.cached_document(row, dataset, url)
                        self.store.set_document_status(
                            dataset,
                            row,
                            url,
                            "complete",
                            digest=digest[2] if digest else None,
                        )
                        result["documents"] += 1
                    except Exception as exc:
                        status = self._document_failure(dataset, row, url, exc)
                        result[
                            "documents_unavailable"
                            if status == "unavailable"
                            else "documents_failed"
                        ] += 1
            except KeyboardInterrupt:
                for future in downloads:
                    future.cancel()
                raise
        return result

    def _download(self, url: str) -> Tuple[bytes, str]:
        client = getattr(self._worker_local, "client", None)
        if client is None:
            client = self.document_client_factory()
            self._worker_local.client = client
            with self._worker_clients_lock:
                self._worker_clients.append(client)
        with self._download_lock:
            remaining = self.download_interval - (self.clock() - self._last_download)
            if remaining > 0:
                time.sleep(remaining)
            self._last_download = self.clock()
        return client.document(url)

    def _document_failure(
        self,
        dataset: str,
        row: Mapping[str, object],
        url: str,
        error: Exception,
    ) -> str:
        status = _document_failure_status(error)
        self.store.error("NSE", dataset, row, error, url)
        self.store.set_document_status(dataset, row, url, status, error=error)
        return status

    def _checkpoint_status(
        self, dataset: str, window_start: date, window_end: date
    ) -> Optional[str]:
        with self.database.engine.connect() as connection:
            return connection.execute(
                select(filing_index_checkpoints.c.status).where(
                    filing_index_checkpoints.c.exchange == "NSE",
                    filing_index_checkpoints.c.dataset == dataset,
                    filing_index_checkpoints.c.window_start == window_start,
                    filing_index_checkpoints.c.window_end == window_end,
                )
            ).scalar_one_or_none()

    def _set_checkpoint(
        self,
        dataset: str,
        window_start: date,
        window_end: date,
        status: str,
        row_count: int = 0,
        processed_count: int = 0,
        failed_count: int = 0,
        error: Optional[str] = None,
        increment_attempt: bool = False,
    ) -> None:
        now = _utcnow()
        values = {
            "exchange": "NSE",
            "dataset": dataset,
            "window_start": window_start,
            "window_end": window_end,
            "status": status,
            "attempts": 1 if increment_attempt else 0,
            "row_count": row_count,
            "processed_count": processed_count,
            "failed_count": failed_count,
            "started_at": now if status == "running" else None,
            "completed_at": now if status in {"complete", "partial", "failed"} else None,
            "error": error,
        }
        statement = sqlite_insert(filing_index_checkpoints).values(**values)
        updates = {
            "status": status,
            "row_count": row_count,
            "processed_count": processed_count,
            "failed_count": failed_count,
            "completed_at": values["completed_at"],
            "error": error,
        }
        if status == "running":
            updates["started_at"] = now
            updates["completed_at"] = None
        if increment_attempt:
            updates["attempts"] = filing_index_checkpoints.c.attempts + 1
        with self.database.transaction() as connection:
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        filing_index_checkpoints.c.exchange,
                        filing_index_checkpoints.c.dataset,
                        filing_index_checkpoints.c.window_start,
                        filing_index_checkpoints.c.window_end,
                    ],
                    set_=updates,
                )
            )

    def _progress(
        self,
        position: int,
        windows: Sequence[Tuple[str, date, date]],
        started: float,
        dataset: str,
        window_start: date,
        window_end: date,
    ) -> None:
        if self.output is None:
            return
        elapsed = max(0.001, self.clock() - started)
        remaining = len(windows) - position
        eta = elapsed / position * remaining
        self.output.write(
            "[{}/{}] {} {}..{} elapsed={:.1f}s eta={:.1f}s\n".format(
                position, len(windows), dataset, window_start, window_end, elapsed, eta
            )
        )
        self.output.flush()

    def _close_worker_clients(self) -> None:
        seen = set()
        for client in self._worker_clients:
            if id(client) in seen or client is self.index_client:
                continue
            seen.add(id(client))
            session = getattr(client, "session", None)
            if session is not None:
                session.close()


class BSEFundamentalBackfill:
    """Resumable per-scrip BSE history for the exchange's non-bulk XBRL index."""

    def __init__(
        self,
        database,
        client: BSEDisclosureClient,
        document_client_factory: Optional[Callable[[], BSEDisclosureClient]] = None,
        workers: int = 3,
        output: Optional[TextIO] = None,
    ) -> None:
        if workers < 1 or workers > 16:
            raise ValueError("workers must be between 1 and 16")
        self.database = database
        self.client = client
        self.document_client_factory = document_client_factory or (lambda: client)
        self.workers = workers
        self.output = output
        self.store = DisclosureStore(database)
        self.collector = NSEDisclosureCollector(
            database, client=client, fetch_documents=False
        )
        self._worker_local = threading.local()
        self._worker_clients: List[BSEDisclosureClient] = []
        self._worker_clients_lock = threading.Lock()

    def run(
        self,
        start: date,
        end: date,
        scrip_codes: Optional[Sequence[str]] = None,
        resume: bool = True,
        retry_failed: bool = False,
        refresh_documents: bool = False,
    ) -> Dict[str, object]:
        if start > end:
            raise ValueError("start must not be after end")
        self.store.initialize()
        symbols = self._symbols(scrip_codes)
        summary: Dict[str, object] = {
            "scrips_total": len(symbols),
            "scrips_completed": 0,
            "scrips_failed": 0,
            "scrips_skipped": 0,
            "index_records": 0,
            "documents": 0,
            "documents_cached": 0,
            "documents_failed": 0,
            "documents_unavailable": 0,
            "interrupted": False,
        }
        started = time.monotonic()
        for position, (code, symbol) in enumerate(symbols, 1):
            status = self._status(code, start, end)
            if resume and status == "complete" and not refresh_documents:
                summary["scrips_skipped"] += 1
                continue
            if retry_failed and status not in {"failed", "partial", "interrupted", None}:
                summary["scrips_skipped"] += 1
                continue
            self._checkpoint(code, start, end, "running", increment_attempt=True)
            try:
                payload = self.client.index("financial_results", code)
                rows = payload.get("Table") or []
                candidates = []
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    row = dict(raw)
                    row.update(symbol=symbol, scrip_code=code)
                    self.store.filing(
                        row, "financial_results_index", exchange="BSE"
                    )
                    summary["index_records"] += 1
                    filed = _date(_first(row, "Fld_CreateDate", "DT_TM"))
                    if filed and not start <= filed <= end:
                        continue
                    candidates.extend(self._xbrl_candidates(row))
                result = self._documents(candidates, refresh_documents)
                for key in (
                    "documents",
                    "documents_cached",
                    "documents_failed",
                    "documents_unavailable",
                ):
                    summary[key] += result[key]
                final = "partial" if result["documents_failed"] else "complete"
                self._checkpoint(
                    code, start, end, final, row_count=len(rows),
                    document_count=result["documents"] + result["documents_cached"],
                    failed_count=result["documents_failed"],
                )
                if final == "complete":
                    summary["scrips_completed"] += 1
                else:
                    summary["scrips_failed"] += 1
            except KeyboardInterrupt:
                self._checkpoint(
                    code, start, end, "interrupted", error="Interrupted by user"
                )
                summary["interrupted"] = True
                break
            except Exception as exc:
                self._checkpoint(code, start, end, "failed", error=str(exc))
                summary["scrips_failed"] += 1
            if self.output is not None:
                elapsed = max(0.001, time.monotonic() - started)
                eta = elapsed / position * (len(symbols) - position)
                self.output.write(
                    "[{}/{}] bse financial_results {} elapsed={:.1f}s eta={:.1f}s\n".format(
                        position, len(symbols), code, elapsed, eta
                    )
                )
                self.output.flush()
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        self._close_worker_clients()
        return summary

    def _symbols(
        self, scrip_codes: Optional[Sequence[str]]
    ) -> List[Tuple[str, str]]:
        with self.database.engine.connect() as connection:
            query = select(
                exchange_symbols.c.scrip_code,
                exchange_symbols.c.exchange_symbol,
            ).where(
                exchange_symbols.c.exchange == "BSE",
                exchange_symbols.c.scrip_code != "",
                exchange_symbols.c.active.is_(True),
            )
            if scrip_codes:
                query = query.where(
                    exchange_symbols.c.scrip_code.in_([str(code) for code in scrip_codes])
                )
            return [(str(code), symbol) for code, symbol in connection.execute(query)]

    @staticmethod
    def _xbrl_candidates(
        row: Mapping[str, object]
    ) -> List[Tuple[Mapping[str, object], str]]:
        candidates = []
        for field, consolidated in (
            ("XMLName", False),
            ("Consol_XMLName", True),
        ):
            path = str(row.get(field) or "").strip()
            if not path.lower().endswith(".xml"):
                continue
            candidate = dict(row)
            candidate["consolidated"] = consolidated
            candidate["xbrl"] = urljoin(BSE_XBRL_BASE, path)
            candidate["seqNumber"] = "BSE:{}:{}:{}:{}:{}".format(
                row.get("scrip_code", ""),
                row.get("quarter_code", ""),
                row.get("Fld_CreateDate", ""),
                "consolidated" if consolidated else "standalone",
                path,
            )
            candidates.append((candidate, candidate["xbrl"]))
        return candidates

    def _documents(
        self,
        candidates: Sequence[Tuple[Mapping[str, object], str]],
        refresh_documents: bool,
    ) -> Dict[str, int]:
        result = {
            "documents": 0,
            "documents_cached": 0,
            "documents_failed": 0,
            "documents_unavailable": 0,
        }
        futures = {}
        with ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="bse-xbrl-download"
        ) as executor:
            for row, url in candidates:
                cached = None if refresh_documents else self.store.cached_document(
                    row, "financial_results", url, exchange="BSE"
                )
                if (
                    cached is None
                    and not refresh_documents
                    and self.store.document_status(
                        "financial_results", row, url, exchange="BSE"
                    )
                    == "unavailable"
                ):
                    result["documents_unavailable"] += 1
                    continue
                if cached:
                    body, content_type, digest = cached
                    try:
                        self.collector.record_with_document(
                            "financial_results", row, body, content_type, url,
                            exchange="BSE",
                        )
                        self.store.set_document_status(
                            "financial_results", row, url, "complete",
                            digest=digest, exchange="BSE",
                        )
                        result["documents_cached"] += 1
                    except Exception as exc:
                        status = self._failure(row, url, exc)
                        result[
                            "documents_unavailable"
                            if status == "unavailable"
                            else "documents_failed"
                        ] += 1
                    continue
                self.store.set_document_status(
                    "financial_results", row, url, "pending",
                    exchange="BSE", increment_attempt=True,
                )
                futures[executor.submit(self._download, url)] = (row, url)
            for future in as_completed(futures):
                row, url = futures[future]
                try:
                    body, content_type = future.result()
                    self.collector.record_with_document(
                        "financial_results", row, body, content_type, url,
                        exchange="BSE",
                    )
                    cached = self.store.cached_document(
                        row, "financial_results", url, exchange="BSE"
                    )
                    self.store.set_document_status(
                        "financial_results", row, url, "complete",
                        digest=cached[2] if cached else None, exchange="BSE",
                    )
                    result["documents"] += 1
                except Exception as exc:
                    status = self._failure(row, url, exc)
                    result[
                        "documents_unavailable"
                        if status == "unavailable"
                        else "documents_failed"
                    ] += 1
        return result

    def _download(self, url: str) -> Tuple[bytes, str]:
        client = getattr(self._worker_local, "client", None)
        if client is None:
            client = self.document_client_factory()
            self._worker_local.client = client
            with self._worker_clients_lock:
                self._worker_clients.append(client)
        return client.document(url)

    def _close_worker_clients(self) -> None:
        seen = set()
        for client in self._worker_clients:
            if id(client) in seen or client is self.client:
                continue
            seen.add(id(client))
            session = getattr(client, "session", None)
            if session is not None:
                session.close()

    def _failure(
        self, row: Mapping[str, object], url: str, error: Exception
    ) -> str:
        status = _document_failure_status(error)
        self.store.error("BSE", "financial_results", row, error, url)
        self.store.set_document_status(
            "financial_results", row, url, status,
            error=error, exchange="BSE",
        )
        return status

    def _status(self, code: str, start: date, end: date) -> Optional[str]:
        with self.database.engine.connect() as connection:
            return connection.scalar(
                select(bse_financial_checkpoints.c.status).where(
                    bse_financial_checkpoints.c.scrip_code == code,
                    bse_financial_checkpoints.c.range_start == start,
                    bse_financial_checkpoints.c.range_end == end,
                )
            )

    def _checkpoint(
        self,
        code: str,
        start: date,
        end: date,
        status: str,
        row_count: int = 0,
        document_count: int = 0,
        failed_count: int = 0,
        error: Optional[str] = None,
        increment_attempt: bool = False,
    ) -> None:
        values = {
            "scrip_code": code,
            "range_start": start,
            "range_end": end,
            "status": status,
            "attempts": 1 if increment_attempt else 0,
            "row_count": row_count,
            "document_count": document_count,
            "failed_count": failed_count,
            "error": error,
            "updated_at": _utcnow(),
        }
        statement = sqlite_insert(bse_financial_checkpoints).values(**values)
        updates = {
            "status": status,
            "row_count": row_count,
            "document_count": document_count,
            "failed_count": failed_count,
            "error": error,
            "updated_at": values["updated_at"],
        }
        if increment_attempt:
            updates["attempts"] = bse_financial_checkpoints.c.attempts + 1
        with self.database.transaction() as connection:
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        bse_financial_checkpoints.c.scrip_code,
                        bse_financial_checkpoints.c.range_start,
                        bse_financial_checkpoints.c.range_end,
                    ],
                    set_=updates,
                )
            )


def coverage_report(database, latest_limit: int = 20) -> Dict[str, object]:
    """Return JSON-safe financial, ownership, document, and checkpoint coverage."""
    DisclosureStore(database).initialize()
    canonical_names = tuple(sorted(DEFAULT_ALIASES))
    with database.engine.connect() as connection:
        financial = connection.execute(
            select(
                func.count(func.distinct(
                    financial_fact_instances.c.symbol
                )).label("symbols"),
                func.count(func.distinct(filings.c.symbol)).label("index_symbols"),
                func.count(func.distinct(filings.c.id)).label("filings"),
                func.count(func.distinct(
                    func.cast(financial_fact_instances.c.filing_id, String)
                    + ":"
                    + func.cast(financial_fact_instances.c.fact_index, String)
                )).label("raw_facts"),
                func.count(func.distinct(financial_metrics.c.metric)).label("canonical_metrics"),
            )
            .select_from(
                filings.outerjoin(
                    financial_fact_instances,
                    financial_fact_instances.c.filing_id == filings.c.id,
                ).outerjoin(
                    financial_metrics, financial_metrics.c.filing_id == filings.c.id
                )
            )
            .where(filings.c.dataset == "financial_results")
        ).mappings().one()

        period_groups = connection.execute(
            select(
                financial_metrics.c.symbol,
                financial_metrics.c.filing_id,
                financial_metrics.c.period_end,
            ).distinct()
        ).all()
        metric_counts = dict(
            connection.execute(
                select(financial_metrics.c.metric, func.count(func.distinct(
                    financial_metrics.c.symbol
                    + ":"
                    + func.cast(financial_metrics.c.filing_id, String)
                    + ":"
                    + func.cast(financial_metrics.c.period_end, String)
                ))).group_by(financial_metrics.c.metric)
            ).all()
        )
        denominator = len(period_groups)
        completeness = {
            name: {
                "periods": int(metric_counts.get(name, 0)),
                "percent": round(100.0 * metric_counts.get(name, 0) / denominator, 2)
                if denominator
                else 0.0,
            }
            for name in canonical_names
        }

        ownership = connection.execute(
            select(
                func.count(func.distinct(shareholding_patterns.c.symbol)).label("symbols"),
                func.count(func.distinct(shareholding_patterns.c.quarter_end)).label("quarters"),
                func.count(func.distinct(
                    shareholding_patterns.c.symbol
                    + ":"
                    + func.cast(shareholding_patterns.c.quarter_end, String)
                )).label("symbol_quarters"),
                func.count(shareholding_patterns.c.filing_id).label("filings"),
                func.sum(case((shareholding_patterns.c.promoter_percent.is_not(None), 1), else_=0)).label("promoter"),
                func.sum(case((shareholding_patterns.c.fii_percent.is_not(None), 1), else_=0)).label("fii"),
                func.sum(case((shareholding_patterns.c.dii_percent.is_not(None), 1), else_=0)).label("dii"),
                func.sum(case((shareholding_patterns.c.public_percent.is_not(None), 1), else_=0)).label("public"),
            )
        ).mappings().one()

        documents = connection.execute(
            select(
                func.count(func.distinct(raw_documents.c.sha256)).filter(
                    filings.c.dataset.in_(BACKFILL_DATASETS)
                ).label("stored"),
                func.count(func.distinct(filings.c.id)).filter(
                    filings.c.exchange == "NSE",
                    filings.c.dataset.in_(BACKFILL_DATASETS),
                    (
                        filings.c.document_url.is_(None)
                        | (filings.c.document_sha256 == "")
                    ),
                ).label("missing"),
            ).select_from(filings.outerjoin(
                raw_documents, filings.c.document_sha256 == raw_documents.c.sha256
            ))
        ).mappings().one()
        broken = connection.execute(
            select(func.count()).select_from(filing_document_status).where(
                filing_document_status.c.status == "failed"
            )
        ).scalar_one()
        checkpoint_rows = connection.execute(
            select(
                filing_index_checkpoints.c.status,
                func.count(),
            ).group_by(filing_index_checkpoints.c.status)
        ).all()
        bse_checkpoint_rows = connection.execute(
            select(
                bse_financial_checkpoints.c.status,
                func.count(),
            ).group_by(bse_financial_checkpoints.c.status)
        ).all()
        latest = connection.execute(
            select(
                filings.c.exchange,
                filings.c.dataset,
                filings.c.symbol,
                filings.c.filing_date,
                filings.c.period_end,
                filings.c.external_id,
                filings.c.is_revision,
                filings.c.document_sha256,
            )
            .order_by(filings.c.filing_date.desc(), filings.c.id.desc())
            .limit(latest_limit)
        ).mappings().all()

    return {
        "financial": {
            **{key: int(value or 0) for key, value in financial.items()},
            "periods": denominator,
            "metric_periods": denominator,
            "canonical_metric_completeness": completeness,
        },
        "ownership": {key: int(value or 0) for key, value in ownership.items()},
        "documents": {
            "stored": int(documents["stored"] or 0),
            "missing": int(documents["missing"] or 0),
            "broken": int(broken or 0),
        },
        "checkpoints": {
            **{"nse:{}".format(status): int(count) for status, count in checkpoint_rows},
            **{"bse:{}".format(status): int(count) for status, count in bse_checkpoint_rows},
        },
        "latest_filings": [
            {
                **dict(row),
                "filing_date": row.filing_date.isoformat() if row.filing_date else None,
                "period_end": row.period_end.isoformat() if row.period_end else None,
            }
            for row in latest
        ],
    }


def format_coverage(report: Mapping[str, object]) -> str:
    financial = report["financial"]
    ownership = report["ownership"]
    documents = report["documents"]
    lines = [
        "Financial: {symbols} symbols, {periods} periods, {filings} filings, "
        "{raw_facts} raw facts, {canonical_metrics} canonical metrics".format(
            **financial
        ),
        "Ownership: {symbols} symbols, {quarters} quarters, {filings} filings "
        "(promoter={promoter}, FII={fii}, DII={dii}, public={public})".format(
            **ownership
        ),
        "Documents: {stored} stored, {missing} missing, {broken} broken".format(
            **documents
        ),
        "Checkpoints: {}".format(
            ", ".join(
                "{}={}".format(key, value)
                for key, value in sorted(report["checkpoints"].items())
            ) or "none"
        ),
        "Canonical completeness:",
    ]
    for metric, values in financial["canonical_metric_completeness"].items():
        lines.append(
            "  {:30s} {:6.2f}% ({})".format(
                metric, values["percent"], values["periods"]
            )
        )
    lines.append("Latest filings:")
    for item in report["latest_filings"]:
        lines.append(
            "  {filing_date} {exchange} {dataset} {symbol} period={period_end} "
            "revision={is_revision} id={external_id}".format(**item)
        )
    return "\n".join(lines)


def dumps_report(report: Mapping[str, object]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)

import threading
import time
from datetime import date

import pytest
from sqlalchemy import func, select

from market_data.database import MarketDatabase
from market_data.disclosures import (
    filing_document_status,
    filing_index_checkpoints,
    filings,
    financial_facts,
    financial_metrics,
    raw_documents,
)
from market_data.fundamental_backfill import FundamentalBackfill, coverage_report
from market_data.fundamental_backfill import BSEFundamentalBackfill


FINANCIAL_XBRL = b"""<?xml version="1.0"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
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
 <fin:ProfitLossForPeriod contextRef="quarter" unitRef="INR">10</fin:ProfitLossForPeriod>
 <fin:RevenueFromOperations contextRef="ytd" unitRef="INR">280</fin:RevenueFromOperations>
</xbrli:xbrl>"""


def financial_row(identifier="fin-1", url="https://example.test/fin-1.xml", **extra):
    return {
        "symbol": "ABC",
        "isin": "INE123A01010",
        "seqNumber": identifier,
        "filingDate": "17-Sep-2026",
        "fromDate": "01-Apr-2026",
        "toDate": "30-Jun-2026",
        "consolidated": "Consolidated",
        "xbrl": url,
        **extra,
    }


class FakeClient:
    def __init__(self, rows=None, failures=None, pause=0):
        self.rows = list(rows or [financial_row()])
        self.failures = dict(failures or {})
        self.pause = pause
        self.index_calls = []
        self.document_calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def index(self, dataset, start=None, end=None, symbol=None):
        self.index_calls.append((dataset, start, end, symbol))
        return list(self.rows)

    def document(self, url):
        with self.lock:
            self.document_calls.append(url)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.pause:
                time.sleep(self.pause)
            remaining = self.failures.get(url, 0)
            if remaining:
                self.failures[url] = remaining - 1
                raise RuntimeError("temporary document failure")
            return FINANCIAL_XBRL.replace(b">100<", (">{}<".format(
                100 + sum(url.encode("utf-8"))
            )).encode()), "application/xml"
        finally:
            with self.lock:
                self.active -= 1


class InterruptingClient(FakeClient):
    def index(self, dataset, start=None, end=None, symbol=None):
        raise KeyboardInterrupt()


class FakeBSEHistoryClient:
    def __init__(self):
        self.document_calls = []

    def index(self, dataset, scrip_code, start=None, end=None):
        return {"Table": [
            {
                "Scrip_cd": int(scrip_code),
                "quarter_code": "JQ2025-2026",
                "Fld_CreateDate": "2025-07-20T12:00:00",
                "XMLName": "results/standalone.xml",
                "Consol_XMLName": "results/consolidated.xml",
            },
            {
                "Scrip_cd": int(scrip_code),
                "quarter_code": "JQ2026-2027",
                "Fld_CreateDate": "2026-07-20T12:00:00",
                "XMLName": "results/current.html",
                "Consol_XMLName": None,
            },
        ]}

    def document(self, url):
        self.document_calls.append(url)
        value = b">100<" if "standalone" in url else b">200<"
        return FINANCIAL_XBRL.replace(b">100<", value), "application/xml"


@pytest.fixture
def database(tmp_path):
    database = MarketDatabase("sqlite:///{}".format(tmp_path / "backfill.db"))
    database.initialize()
    yield database
    database.engine.dispose()


def runner(database, client, workers=2):
    return FundamentalBackfill(
        database,
        client,
        document_client_factory=lambda: client,
        workers=workers,
        download_interval=0,
    )


def test_window_checkpoints_resume_and_bulk_index(database):
    client = FakeClient()
    backfill = runner(database, client)
    first = backfill.run(
        date(2026, 1, 1),
        date(2026, 1, 4),
        datasets=("financial_results",),
        window_days=2,
    )
    second = backfill.run(
        date(2026, 1, 1),
        date(2026, 1, 4),
        datasets=("financial_results",),
        window_days=2,
    )

    assert first["windows_completed"] == 2
    assert second["windows_skipped"] == 2
    assert len(client.index_calls) == 2
    assert all(call[3] is None for call in client.index_calls)
    with database.engine.connect() as connection:
        checkpoints = connection.execute(
            select(filing_index_checkpoints)
        ).mappings().all()
    assert len(checkpoints) == 2
    assert {row["status"] for row in checkpoints} == {"complete"}


def test_revision_documents_are_preserved_and_rerun_uses_cache(database):
    rows = [
        financial_row(
            "same", "https://example.test/revision-1.xml",
            revisionDate="2026-07-01",
        ),
        financial_row(
            "same", "https://example.test/revision-2.xml",
            revisionDate="2026-07-02",
        ),
    ]
    client = FakeClient(rows)
    backfill = runner(database, client)
    backfill.run(
        date(2026, 7, 1), date(2026, 7, 1),
        datasets=("financial_results",),
    )
    first_downloads = len(client.document_calls)
    second = backfill.run(
        date(2026, 7, 1), date(2026, 7, 1),
        datasets=("financial_results",),
        resume=False,
    )
    assert second["documents_cached"] == 2
    assert len(client.document_calls) == first_downloads
    with database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(filings)) == 2
        assert connection.scalar(select(func.count()).select_from(raw_documents)) == 2


def test_failed_document_continues_and_retry_only_fetches_failure(database):
    good = financial_row("good", "https://example.test/good.xml")
    bad = financial_row("bad", "https://example.test/bad.xml")
    client = FakeClient([good, bad], failures={bad["xbrl"]: 1})
    backfill = runner(database, client)
    first = backfill.run(
        date(2026, 7, 1), date(2026, 7, 1),
        datasets=("financial_results",),
    )
    second = backfill.run(
        date(2026, 7, 1), date(2026, 7, 1),
        datasets=("financial_results",),
        retry_failed=True,
    )

    assert first["documents"] == 1
    assert first["documents_failed"] == 1
    assert second["documents"] == 1
    assert second["documents_cached"] == 1
    assert client.document_calls.count(good["xbrl"]) == 1
    assert client.document_calls.count(bad["xbrl"]) == 2
    with database.engine.connect() as connection:
        statuses = connection.execute(
            select(filing_document_status.c.status)
        ).scalars().all()
        attempts = dict(connection.execute(
            select(
                filing_document_status.c.document_url,
                filing_document_status.c.attempts,
            )
        ).all())
        checkpoint = connection.execute(
            select(filing_index_checkpoints.c.status)
        ).scalar_one()
    assert statuses == ["complete", "complete"] or sorted(statuses) == ["complete", "complete"]
    assert attempts[good["xbrl"]] == 1
    assert attempts[bad["xbrl"]] == 2
    assert checkpoint == "complete"


def test_quarter_context_coverage_and_idempotency(database):
    client = FakeClient()
    backfill = runner(database, client)
    for _ in range(2):
        backfill.run(
            date(2026, 7, 1), date(2026, 7, 1),
            datasets=("financial_results",),
            resume=False,
        )

    with database.engine.connect() as connection:
        revenue = connection.execute(
            select(financial_metrics.c.value).where(
                financial_metrics.c.metric == "revenue"
            )
        ).scalar_one()
        assert revenue != 280
        assert connection.scalar(select(func.count()).select_from(filings)) == 1
        assert connection.scalar(select(func.count()).select_from(raw_documents)) == 1
        assert connection.scalar(select(func.count()).select_from(financial_facts)) == 3

    report = coverage_report(database, latest_limit=1)
    assert report["financial"]["symbols"] == 1
    assert report["financial"]["filings"] == 1
    assert report["financial"]["raw_facts"] == 3
    assert report["financial"]["canonical_metric_completeness"]["revenue"] == {
        "periods": 1,
        "percent": 100.0,
    }
    assert report["documents"] == {"stored": 1, "missing": 0, "broken": 0}


def test_bounded_download_concurrency_has_serialized_safe_writes(database):
    rows = [
        financial_row("fin-{}".format(index), "https://example.test/{}.xml".format(index))
        for index in range(6)
    ]
    client = FakeClient(rows, pause=0.03)
    result = runner(database, client, workers=3).run(
        date(2026, 7, 1), date(2026, 7, 1),
        datasets=("financial_results",),
    )

    assert result["documents"] == 6
    assert client.max_active <= 3
    assert client.max_active > 1
    with database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(filings)) == 6
        assert connection.scalar(select(func.count()).select_from(raw_documents)) == 6


def test_graceful_interrupt_marks_window_for_resume(database):
    result = runner(database, InterruptingClient()).run(
        date(2026, 7, 1), date(2026, 7, 1),
        datasets=("financial_results",),
    )

    assert result["interrupted"] is True
    with database.engine.connect() as connection:
        status = connection.scalar(select(filing_index_checkpoints.c.status))
    assert status == "interrupted"


def test_bse_per_scrip_xbrl_history_preserves_raw_index_and_cache(database):
    database.upsert_securities([{
        "exchange": "BSE",
        "symbol": "ABC",
        "scrip_code": "500001",
        "isin": "INE123A01010",
        "name": "ABC Limited",
        "source": "test",
    }])
    client = FakeBSEHistoryClient()
    backfill = BSEFundamentalBackfill(
        database,
        client,
        document_client_factory=lambda: client,
        workers=2,
    )
    first = backfill.run(
        date(2018, 1, 1), date(2026, 9, 18), scrip_codes=("500001",)
    )
    second = backfill.run(
        date(2018, 1, 1),
        date(2026, 9, 18),
        scrip_codes=("500001",),
        resume=False,
    )
    broader = backfill.run(
        date(2017, 1, 1),
        date(2026, 9, 18),
        scrip_codes=("500001",),
    )

    assert first["index_records"] == 2
    assert first["documents"] == 2
    assert second["documents_cached"] == 2
    assert broader["scrips_completed"] == 1
    assert len(client.document_calls) == 2
    with database.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(filings).where(
                filings.c.dataset == "financial_results_index"
            )
        ) == 2
        assert connection.scalar(
            select(func.count()).select_from(filings).where(
                filings.c.exchange == "BSE",
                filings.c.dataset == "financial_results",
            )
        ) == 2

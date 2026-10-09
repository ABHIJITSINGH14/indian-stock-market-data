import hashlib
import json
import sqlite3
from pathlib import Path

from scripts.aaru_adapt_database import adapt_database
from scripts.aaru_inventory import inventory_database, inventory_screener
from scripts.aaru_missing_manifest import build_missing_items


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_source_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE securities(
            id INTEGER PRIMARY KEY,
            canonical_id TEXT NOT NULL,
            isin TEXT,
            name TEXT NOT NULL,
            active INTEGER NOT NULL
        );
        CREATE TABLE exchange_symbols(
            id INTEGER PRIMARY KEY,
            security_id INTEGER NOT NULL,
            exchange TEXT NOT NULL,
            exchange_symbol TEXT NOT NULL,
            series TEXT NOT NULL,
            scrip_code TEXT NOT NULL,
            active INTEGER NOT NULL,
            source TEXT NOT NULL,
            FOREIGN KEY(security_id) REFERENCES securities(id)
        );
        CREATE TABLE daily_prices(
            security_id INTEGER NOT NULL,
            exchange TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            series TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            last REAL, previous_close REAL,
            volume INTEGER, turnover REAL, trades INTEGER,
            deliverable_quantity INTEGER,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(security_id) REFERENCES securities(id)
        );
        CREATE TABLE ingestion_runs(
            id INTEGER PRIMARY KEY, source TEXT, dataset TEXT, started_at TEXT,
            finished_at TEXT, status TEXT, attempted INTEGER, succeeded INTEGER,
            failed INTEGER, rows_written INTEGER, message TEXT
        );
        CREATE TABLE ingestion_checkpoints(
            source TEXT, dataset TEXT, checkpoint_key TEXT,
            checkpoint_date TEXT, status TEXT, row_count INTEGER,
            error TEXT, updated_at TEXT
        );
        CREATE TABLE ingestion_errors(
            id INTEGER PRIMARY KEY, run_id INTEGER, source TEXT, dataset TEXT,
            checkpoint_key TEXT, error_type TEXT, message TEXT, created_at TEXT
        );
        CREATE TABLE archive_availability(
            source TEXT, year_month TEXT, status TEXT,
            first_available_date TEXT, last_available_date TEXT,
            success_count INTEGER, not_published_count INTEGER, updated_at TEXT
        );
        CREATE TABLE filings(
            id INTEGER PRIMARY KEY, security_id INTEGER, symbol TEXT,
            exchange TEXT, dataset TEXT, external_id TEXT, filing_date TEXT,
            period_start TEXT, period_end TEXT, document_url TEXT,
            document_sha256 TEXT, is_revision INTEGER, created_at TEXT,
            FOREIGN KEY(security_id) REFERENCES securities(id)
        );
        CREATE TABLE raw_documents(
            sha256 TEXT PRIMARY KEY, filing_id INTEGER, url TEXT,
            retrieved_at TEXT, content_type TEXT, body BLOB,
            FOREIGN KEY(filing_id) REFERENCES filings(id)
        );
        CREATE TABLE shareholding_patterns(
            filing_id INTEGER PRIMARY KEY, security_id INTEGER, symbol TEXT,
            quarter_end TEXT, promoter_percent REAL, fii_percent REAL,
            dii_percent REAL, public_percent REAL,
            non_institution_public_percent REAL,
            FOREIGN KEY(filing_id) REFERENCES filings(id),
            FOREIGN KEY(security_id) REFERENCES securities(id)
        );
        CREATE TABLE financial_facts(id INTEGER PRIMARY KEY);
        CREATE TABLE financial_fact_instances(
            filing_id INTEGER, fact_index INTEGER, security_id INTEGER,
            symbol TEXT, concept TEXT, namespace TEXT, context_id TEXT,
            entity_identifier TEXT, entity_scheme TEXT, period_kind TEXT,
            period_start TEXT, period_end TEXT, instant TEXT,
            dimensions_json TEXT, unit TEXT, decimals TEXT, precision TEXT,
            nil INTEGER, value_text TEXT, value_numeric REAL
        );
        CREATE TABLE financial_metrics(
            filing_id INTEGER, symbol TEXT, filing_date TEXT, period_end TEXT,
            metric TEXT, value REAL, source TEXT, scope TEXT, derivation TEXT
        );
        CREATE TABLE corporate_actions(
            filing_id INTEGER PRIMARY KEY, security_id INTEGER, symbol TEXT,
            action_type TEXT, subject TEXT, ex_date TEXT, record_date TEXT,
            face_value REAL, amount REAL
        );
        CREATE TABLE board_meetings(filing_id INTEGER PRIMARY KEY);
        CREATE TABLE pit_disclosures(filing_id INTEGER PRIMARY KEY);
        CREATE TABLE sast_disclosures(filing_id INTEGER PRIMARY KEY);
        CREATE TABLE institutional_activity(
            trade_date TEXT, category TEXT, source TEXT, buy_value REAL,
            sell_value REAL, net_value REAL, unit TEXT, raw_json TEXT
        );
        CREATE TABLE market_metrics(symbol TEXT, as_of TEXT, metric TEXT, value REAL);
        CREATE TABLE disclosure_errors(id INTEGER PRIMARY KEY);
        CREATE TABLE filing_index_checkpoints(
            exchange TEXT, dataset TEXT, window_start TEXT, window_end TEXT,
            status TEXT, attempts INTEGER, row_count INTEGER,
            processed_count INTEGER, failed_count INTEGER,
            started_at TEXT, completed_at TEXT, error TEXT
        );
        CREATE TABLE filing_document_status(
            exchange TEXT, dataset TEXT, external_id TEXT, document_url TEXT,
            status TEXT, attempts INTEGER, document_sha256 TEXT,
            error_type TEXT, error TEXT, updated_at TEXT
        );
        CREATE TABLE bse_financial_checkpoints(
            scrip_code TEXT, range_start TEXT, range_end TEXT, status TEXT,
            attempts INTEGER, row_count INTEGER, document_count INTEGER,
            failed_count INTEGER, error TEXT, updated_at TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO securities VALUES(1,'ISIN:INE000000001','INE000000001','Example Ltd',1)"
    )
    connection.execute(
        "INSERT INTO exchange_symbols VALUES(1,1,'NSE','EXAMPLE','EQ','',1,'nse_master')"
    )
    connection.execute(
        "INSERT INTO daily_prices VALUES(1,'NSE','2026-09-18','EQ',10,12,9,11,11,10,100,1000,5,50,'nse_bhavcopy','2026-09-18','2026-09-18')"
    )
    connection.execute(
        "INSERT INTO ingestion_checkpoints VALUES('nse','daily_prices','2026-09-17','2026-09-17','failed',0,'timeout','2026-09-18')"
    )
    connection.execute(
        "INSERT INTO filings VALUES(1,1,'EXAMPLE','NSE','financial_results','f1','2026-08-01','2026-04-01','2026-06-30','https://example/x.xml','',0,'2026-08-01')"
    )
    connection.execute(
        "INSERT INTO filing_document_status VALUES('NSE','financial_results','f1','https://example/x.xml','failed',2,NULL,'Timeout','timeout','2026-09-18')"
    )
    connection.execute(
        "INSERT INTO filing_index_checkpoints VALUES('NSE','financial_results','2026-07-01','2026-07-31','partial',1,1,1,1,'2026-09-18','2026-09-18','timeout')"
    )
    connection.execute(
        "INSERT INTO financial_metrics VALUES(1,'EXAMPLE','2026-08-01','2026-06-30','revenue',100,'reported','standalone',NULL)"
    )
    connection.execute(
        "INSERT INTO shareholding_patterns VALUES(1,1,'EXAMPLE','2026-06-30',70,5,10,15,0)"
    )
    connection.commit()
    connection.close()


def test_inventory_manifest_and_adaptation(tmp_path: Path) -> None:
    source = tmp_path / "stock_market.db"
    create_source_database(source)
    source_hash = file_hash(source)

    screener_root = tmp_path / "screener"
    manifest_dir = screener_root / "MANIFESTS" / "SCREENER_RESUME_V3"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "summary.json").write_text(
        json.dumps({"universe_entries": 3, "run_state": "PAUSED"})
    )
    (manifest_dir / "coverage.csv").write_text(
        "key,status\nA,pages_collected\nB,pending\nC,partial\n"
    )

    inventory = inventory_database(source, integrity_mode="quick")
    inventory["screener"] = inventory_screener(screener_root)
    assert inventory["integrity_check"] == ["ok"]
    assert inventory["price_coverage"][0][0] == "NSE"
    assert inventory["screener"]["statuses"]["pending"] == 1

    missing = build_missing_items(inventory)
    assert any(
        value["source"] == "NSE"
        and value["dataset"] == "cash_eod"
        and value["status"] == "retryable"
        for value in missing
    )
    assert any(
        value["source"] == "SCREENER"
        and value["status"] == "retryable"
        for value in missing
    )

    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(inventory))
    manifest_path = tmp_path / "missing.json"
    manifest_path.write_text(json.dumps({"items": missing}))
    destination = tmp_path / "aaru_candidate.sqlite"
    schema = Path(__file__).resolve().parents[1] / "schema" / "aaru_market_staging.sql"

    result = adapt_database(
        source,
        destination,
        schema,
        inventory_path=inventory_path,
        missing_manifest_path=manifest_path,
        source_commit_sha="test-commit",
    )
    assert destination.exists()
    assert source_hash == file_hash(source)
    assert result["integrity"] == "ok"

    connection = sqlite3.connect(destination)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM aaru_source_snapshots"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM aaru_missing_manifest"
        ).fetchone()[0] >= 2
        assert connection.execute(
            "SELECT COUNT(*) FROM aaru_cash_eod"
        ).fetchone()[0] == 1
        assert connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0] == "ok"
    finally:
        connection.close()

import csv
import json
import sqlite3
from pathlib import Path

from scripts.aaru_adapt_database import adapt_database
from scripts.aaru_inventory import inventory_database, inventory_screener
from scripts.aaru_missing_manifest import build_items


def make_db(path: Path) -> None:
    c = sqlite3.connect(path)
    c.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE securities(id INTEGER PRIMARY KEY,canonical_id TEXT,isin TEXT,name TEXT,active INTEGER);
        CREATE TABLE exchange_symbols(id INTEGER PRIMARY KEY,security_id INTEGER,exchange TEXT,exchange_symbol TEXT,series TEXT,scrip_code TEXT,active INTEGER,source TEXT);
        CREATE TABLE daily_prices(security_id INTEGER,exchange TEXT,trading_date TEXT,series TEXT,open REAL,high REAL,low REAL,close REAL,last REAL,previous_close REAL,volume INTEGER,turnover REAL,trades INTEGER,deliverable_quantity INTEGER,source TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE ingestion_checkpoints(source TEXT,dataset TEXT,checkpoint_key TEXT,checkpoint_date TEXT,status TEXT,row_count INTEGER,error TEXT,updated_at TEXT);
        CREATE TABLE filing_index_checkpoints(exchange TEXT,dataset TEXT,window_start TEXT,window_end TEXT,status TEXT,attempts INTEGER,row_count INTEGER,processed_count INTEGER,failed_count INTEGER,started_at TEXT,completed_at TEXT,error TEXT);
        CREATE TABLE filing_document_status(exchange TEXT,dataset TEXT,external_id TEXT,document_url TEXT,status TEXT,attempts INTEGER,document_sha256 TEXT,error_type TEXT,error TEXT,updated_at TEXT);
        CREATE TABLE bse_financial_checkpoints(scrip_code TEXT,range_start TEXT,range_end TEXT,status TEXT,attempts INTEGER,row_count INTEGER,document_count INTEGER,failed_count INTEGER,error TEXT,updated_at TEXT);
        CREATE TABLE filings(id INTEGER PRIMARY KEY,security_id INTEGER,symbol TEXT,exchange TEXT,dataset TEXT,external_id TEXT,filing_date TEXT,period_start TEXT,period_end TEXT,document_url TEXT,document_sha256 TEXT,is_revision INTEGER,created_at TEXT);
        CREATE TABLE raw_documents(sha256 TEXT PRIMARY KEY,filing_id INTEGER,url TEXT,retrieved_at TEXT,content_type TEXT,body BLOB);
        CREATE TABLE financial_fact_instances(filing_id INTEGER,fact_index INTEGER,security_id INTEGER,symbol TEXT,concept TEXT,namespace TEXT,context_id TEXT,entity_identifier TEXT,entity_scheme TEXT,period_kind TEXT,period_start TEXT,period_end TEXT,instant TEXT,dimensions_json TEXT,unit TEXT,decimals TEXT,precision TEXT,nil INTEGER,value_text TEXT,value_numeric REAL);
        CREATE TABLE financial_metrics(filing_id INTEGER,symbol TEXT,filing_date TEXT,period_end TEXT,metric TEXT,value REAL,source TEXT,scope TEXT,derivation TEXT);
        CREATE TABLE shareholding_patterns(filing_id INTEGER,security_id INTEGER,symbol TEXT,quarter_end TEXT,promoter_percent REAL,fii_percent REAL,dii_percent REAL,public_percent REAL,non_institution_public_percent REAL);
        CREATE TABLE corporate_actions(filing_id INTEGER);
        CREATE TABLE pit_disclosures(filing_id INTEGER);
        CREATE TABLE sast_disclosures(filing_id INTEGER);
        """
    )
    c.execute("INSERT INTO securities VALUES(1,'c','INE000000001','Example',1)")
    c.execute("INSERT INTO exchange_symbols VALUES(1,1,'NSE','EX','EQ','',1,'nse')")
    c.execute("INSERT INTO daily_prices VALUES(1,'NSE','2026-09-18','EQ',1,1,1,1,1,1,1,1,1,1,'nse','x','x')")
    c.execute("INSERT INTO ingestion_checkpoints VALUES('nse','daily_prices','2026-09-17','2026-09-17','failed',0,'timeout','x')")
    c.execute("INSERT INTO filing_document_status VALUES('NSE','financial_results','f1','https://x','failed',2,NULL,'Timeout','timeout','x')")
    c.commit()
    c.close()


def make_screener(root: Path) -> None:
    d = root / "MANIFESTS/SCREENER_RESUME_V3"
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps({"universe_entries": 3}))
    with (d / "coverage.csv").open("w", newline="") as h:
        w = csv.writer(h)
        w.writerow(["key", "status", "reason"])
        w.writerow(["A", "pages_collected", ""])
        w.writerow(["B", "pending", "not tried"])
        w.writerow(["C", "partial", "standalone only"])


def test_live_inventory_manifest_and_metadata_adaptation(tmp_path: Path) -> None:
    source = tmp_path / "stock.db"
    make_db(source)
    screener = tmp_path / "screener"
    make_screener(screener)
    out = tmp_path / "inventory"
    inventory = inventory_database(
        source, profile="live", integrity_mode="none",
        detail_directory=out / "details"
    )
    inventory["screener"] = inventory_screener(
        screener, detail_output=out / "details/screener_coverage.csv"
    )
    assert inventory["integrity_check"] == ["not_run"]
    assert inventory["profile"] == "live"
    assert inventory["table_counts"]["daily_prices"]["method"] == "max_rowid_upper_bound"

    contracts = json.loads(
        (Path(__file__).parents[1] / "config/aaru_dataset_contracts.json").read_text()
    )
    items = build_items(inventory, contracts)
    assert any(x["source"] == "NSE" and x["dataset"] == "cash_eod" for x in items)
    assert any(x["source"] == "SCREENER" and x["identity"] == "B" for x in items)
    assert any(x["dataset"] == "bulk_block_deals" for x in items)

    inv = tmp_path / "inventory.json"
    inv.write_text(json.dumps(inventory))
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps({"items": items}))
    candidate = tmp_path / "candidate.sqlite"
    result = adapt_database(
        source, candidate,
        Path(__file__).parents[1] / "schema/aaru_market_staging.sql",
        mode="metadata", inventory_path=inv, missing_manifest_path=man
    )
    assert result["mode"] == "metadata"
    c = sqlite3.connect(candidate)
    assert c.execute("SELECT COUNT(*) FROM aaru_source_snapshots").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM aaru_missing_manifest").fetchone()[0] > 0
    assert c.execute("SELECT name FROM sqlite_master WHERE name='daily_prices'").fetchone() is None
    c.close()


def test_snapshot_full_copy_and_conditional_views(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    make_db(source)
    destination = tmp_path / "full.sqlite"
    result = adapt_database(
        source, destination,
        Path(__file__).parents[1] / "schema/aaru_market_staging.sql",
        mode="full", reserve_gib=0
    )
    assert result["integrity"] == "ok"
    c = sqlite3.connect(destination)
    assert c.execute("SELECT COUNT(*) FROM aaru_cash_eod").fetchone()[0] == 1
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()

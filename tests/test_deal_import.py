import hashlib
import json
import sqlite3

from sqlalchemy import func, select

from market_data.database import MarketDatabase, market_deals
from market_data.deal_import import DealImportError, import_sealed_deals


def _source(tmp_path, csv=False, side="BUY"):
    source = tmp_path / "market.db"
    connection = sqlite3.connect(source)
    connection.execute(
        "CREATE TABLE deals("
        "rec_sha TEXT PRIMARY KEY, kind TEXT, symbol TEXT, deal_date TEXT, "
        "rec_json TEXT, src_sha256 TEXT)"
    )
    raw = {
        "BD_SYMBOL": "ABC",
        "BD_DT_DATE": "02-JAN-2024",
        "BD_CLIENT_NAME": "Client",
        "BD_BUY_SELL": side,
        "BD_QTY_TRD": 10,
        "BD_TP_WATP": 12.5,
        "BD_REMARKS": "-",
    }
    payload = json.dumps(raw, sort_keys=True)
    kind = "bulk_csv" if csv else "api_bulk"
    record_input = ("api_" + kind + payload) if csv else payload
    connection.execute(
        "INSERT INTO deals VALUES(?,?,?,?,?,?)",
        (
            hashlib.sha256(record_input.encode()).hexdigest(),
            kind,
            "ABC",
            "02-JAN-2024",
            payload,
            "b" * 64,
        ),
    )
    connection.commit()
    connection.close()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"kind":"api_bulk_v2"}\n')
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    (tmp_path / "manifest.sha256").write_text(digest + "  manifest.jsonl\n")
    return source, manifest


def test_import_sealed_deals_is_verified_and_idempotent(tmp_path):
    source, manifest = _source(tmp_path)
    database = MarketDatabase("sqlite:///{}".format(tmp_path / "canonical.db"))
    database.initialize()
    database.upsert_securities(
        [
            {
                "exchange": "NSE",
                "symbol": "ABC",
                "series": "EQ",
                "name": "ABC Limited",
                "source": "fixture",
            }
        ]
    )
    first = import_sealed_deals(database, source, manifest)
    second = import_sealed_deals(database, source, manifest)
    assert first["rows_written"] == 1
    assert first["security_linked"] == 1
    assert second["rows_written"] == 1
    with database.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(market_deals)) == 1


def test_import_accepts_verified_csv_recovery_rows(tmp_path):
    source, manifest = _source(tmp_path, csv=True)
    database = MarketDatabase("sqlite:///{}".format(tmp_path / "canonical.db"))
    result = import_sealed_deals(database, source, manifest)
    assert result["bulk_rows"] == 1
    with database.engine.connect() as connection:
        assert connection.scalar(select(market_deals.c.source)) == (
            "nse_official_deals_csv"
        )


def test_import_normalizes_historical_side_labels(tmp_path):
    for index, (source_side, expected_side) in enumerate(
        (("B", "BUY"), ("S", "SELL"), ("Sold", "SELL"))
    ):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        source, manifest = _source(case_path, side=source_side)
        database = MarketDatabase(
            "sqlite:///{}".format(case_path / "canonical.db")
        )
        import_sealed_deals(database, source, manifest)
        with database.engine.connect() as connection:
            assert connection.scalar(select(market_deals.c.side)) == expected_side


def test_import_rejects_unsealed_manifest(tmp_path):
    source, manifest = _source(tmp_path)
    manifest.write_text("changed\n")
    database = MarketDatabase("sqlite:///{}".format(tmp_path / "canonical.db"))
    try:
        import_sealed_deals(database, source, manifest)
    except DealImportError as error:
        assert "SHA-256" in str(error)
    else:
        raise AssertionError("Expected an invalid seal to be rejected")

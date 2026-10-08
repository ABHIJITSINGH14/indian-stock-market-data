"""Import sealed official NSE bulk and block deals into the canonical database."""

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Mapping, Optional

from sqlalchemy import case, select

from market_data.database import MarketDatabase, exchange_symbols
from market_data.normalization import clean_text, normalize_symbol


class DealImportError(ValueError):
    """Raised when the sealed deal source fails provenance or content checks."""


def verify_sealed_manifest(manifest_path: Path, checksum_path: Path) -> str:
    expected_parts = checksum_path.read_text(encoding="utf-8").strip().split()
    if not expected_parts or len(expected_parts[0]) != 64:
        raise DealImportError("Manifest checksum sidecar is invalid")
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if digest != expected_parts[0].lower():
        raise DealImportError("Manifest SHA-256 does not match its checksum sidecar")
    return digest


def _security_map(database: MarketDatabase) -> Dict[str, int]:
    preference = case((exchange_symbols.c.series == "EQ", 0), else_=1)
    with database.engine.connect() as connection:
        rows = connection.execute(
            select(
                exchange_symbols.c.normalized_symbol,
                exchange_symbols.c.security_id,
            )
            .where(exchange_symbols.c.exchange == "NSE")
            .order_by(
                exchange_symbols.c.normalized_symbol,
                preference,
                exchange_symbols.c.active.desc(),
                exchange_symbols.c.updated_at.desc(),
            )
        )
        result: Dict[str, int] = {}
        for symbol, security_id in rows:
            result.setdefault(symbol, int(security_id))
        return result


def _parse_record(
    row: Mapping[str, object], security_ids: Mapping[str, int]
) -> Dict[str, object]:
    try:
        raw = json.loads(str(row["rec_json"]))
        record_sha = clean_text(row["rec_sha"]).lower()
        source_sha = clean_text(row["src_sha256"]).lower()
        kind = clean_text(row["kind"])
        deal_type = {
            "api_bulk": "bulk",
            "api_block": "block",
            "bulk_csv": "bulk",
            "block_csv": "block",
        }[kind]
        symbol = normalize_symbol(row["symbol"], "NSE")
        deal_date = datetime.strptime(clean_text(row["deal_date"]), "%d-%b-%Y").date()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DealImportError("Official deal row is malformed: {}".format(row)) from error
    if len(record_sha) != 64 or len(source_sha) != 64 or not symbol:
        raise DealImportError("Official deal row has invalid identity or provenance")
    canonical_raw = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    source_json = json.dumps(raw, sort_keys=True)
    hash_input = source_json if kind.startswith("api_") else "api_" + kind + source_json
    calculated_sha = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
    if calculated_sha != record_sha:
        raise DealImportError("Official deal record SHA-256 does not match payload")
    raw_side = clean_text(raw.get("BD_BUY_SELL") or raw.get("Buy / Sell")).upper()
    side = {
        "B": "BUY",
        "BUY": "BUY",
        "BOUGHT": "BUY",
        "S": "SELL",
        "SELL": "SELL",
        "SOLD": "SELL",
    }.get(raw_side)
    if raw_side and side is None:
        raise DealImportError("Official deal row has invalid buy/sell value")
    quantity = raw.get("BD_QTY_TRD")
    if quantity is None:
        quantity = clean_text(raw.get("Quantity Traded")).replace(",", "") or None
    price = raw.get("BD_TP_WATP")
    if price is None:
        price = (
            clean_text(raw.get("Trade Price / Wght. Avg. Price")).replace(",", "")
            or None
        )
    source = (
        "nse_official_deals_csv"
        if kind.endswith("_csv")
        else "nse_official_deals_archive"
    )
    return {
        "record_sha256": record_sha,
        "security_id": security_ids.get(symbol),
        "exchange": "NSE",
        "deal_type": deal_type,
        "deal_date": deal_date,
        "symbol": symbol,
        "client_name": clean_text(
            raw.get("BD_CLIENT_NAME") or raw.get("Client Name")
        )
        or None,
        "side": side,
        "quantity": int(quantity) if quantity is not None else None,
        "price": float(price) if price is not None else None,
        "remarks": clean_text(raw.get("BD_REMARKS") or raw.get("Remarks")) or None,
        "source": source,
        "source_sha256": source_sha,
        "raw_data": canonical_raw,
    }


def import_sealed_deals(
    database: MarketDatabase,
    source_database: Path,
    manifest_path: Path,
    checksum_path: Optional[Path] = None,
    batch_size: int = 1000,
) -> Dict[str, object]:
    checksum_path = checksum_path or manifest_path.with_suffix(".sha256")
    manifest_sha = verify_sealed_manifest(manifest_path, checksum_path)
    if batch_size < 1:
        raise DealImportError("Batch size must be positive")
    database.initialize()
    security_ids = _security_map(database)
    source = sqlite3.connect(
        "file:{}?mode=ro".format(source_database.resolve()), uri=True, timeout=30
    )
    source.row_factory = sqlite3.Row
    written = 0
    linked = 0
    counts = {"bulk": 0, "block": 0}
    try:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise DealImportError("Official deal source database failed quick_check")
        relation = (
            "v_deals"
            if source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='view' AND name='v_deals'"
            ).fetchone()
            else "deals"
        )
        cursor = source.execute(
            "SELECT rec_sha, kind, symbol, deal_date, rec_json, src_sha256 "
            "FROM {} ORDER BY rec_sha".format(relation)
        )
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            records = [_parse_record(row, security_ids) for row in rows]
            written += database.upsert_market_deals(records)
            linked += sum(record["security_id"] is not None for record in records)
            for record in records:
                counts[str(record["deal_type"])] += 1
    finally:
        source.close()
    return {
        "rows_written": written,
        "security_linked": linked,
        "security_unlinked": written - linked,
        "bulk_rows": counts["bulk"],
        "block_rows": counts["block"],
        "manifest_sha256": manifest_sha,
    }

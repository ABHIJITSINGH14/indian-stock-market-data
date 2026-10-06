"""Import checksum-manifested official bhavcopies already stored on disk."""

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from market_data.database import (
    MarketDatabase,
    daily_prices,
    local_archive_assets,
    local_archive_imports,
    utcnow,
)
from market_data.sources import parse_bse_bhavcopy, parse_nse_bhavcopy


GIB = 1024 ** 3


class LocalArchiveError(ValueError):
    """Raised when local archive provenance or contents are invalid."""


@dataclass(frozen=True)
class ManifestEntry:
    kind: str
    trading_date: date
    url: str
    sha256: str
    size_bytes: int
    fetched_utc: str
    path: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(
    manifest_path: Path,
    raw_root: Path,
    kinds: Iterable[str] = ("nse_cm", "bse_cm"),
) -> List[ManifestEntry]:
    """Load a manifest and resolve each entry to its expected immutable file."""

    selected = set(kinds)
    entries: Dict[tuple, ManifestEntry] = {}
    manifest_path = manifest_path.expanduser().resolve()
    raw_root = raw_root.expanduser().resolve()
    for line_number, line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise LocalArchiveError(
                "invalid manifest JSON on line {}".format(line_number)
            ) from error
        kind = record.get("kind")
        if kind not in selected:
            continue
        try:
            trading_date = date.fromisoformat(record["tag"])
            basename = Path(urlparse(record["url"]).path).name
            path = raw_root / kind / "{}_{}".format(record["tag"], basename)
            entry = ManifestEntry(
                kind=kind,
                trading_date=trading_date,
                url=record["url"],
                sha256=record["sha256"].lower(),
                size_bytes=int(record["bytes"]),
                fetched_utc=record["fetched_utc"],
                path=path,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise LocalArchiveError(
                "invalid {} record on line {}".format(kind, line_number)
            ) from error
        key = (kind, trading_date)
        previous = entries.get(key)
        if previous and previous.sha256 != entry.sha256:
            raise LocalArchiveError(
                "conflicting manifest revisions for {} {}".format(kind, trading_date)
            )
        entries[key] = entry
    return sorted(entries.values(), key=lambda item: (item.trading_date, item.kind))


class LocalArchiveImporter:
    def __init__(
        self,
        database: MarketDatabase,
        min_free_bytes: int = 12 * GIB,
    ):
        self.database = database
        self.min_free_bytes = min_free_bytes

    def catalog_entries(
        self, entries: Iterable[ManifestEntry], batch_size: int = 500
    ) -> Mapping[str, int]:
        """Verify external files and register them without materializing rows."""

        self.database.initialize()
        verified = 0
        values = []
        for entry in entries:
            self._verify(entry)
            values.append(
                {
                    "kind": entry.kind,
                    "trading_date": entry.trading_date,
                    "sha256": entry.sha256,
                    "source_url": entry.url,
                    "local_path": str(entry.path),
                    "size_bytes": entry.size_bytes,
                    "fetched_utc": entry.fetched_utc,
                    "status": "verified_external",
                    "verified_at": utcnow(),
                }
            )
            if len(values) >= batch_size:
                self._upsert_assets(values)
                verified += len(values)
                values = []
        if values:
            self._upsert_assets(values)
            verified += len(values)
        return {"verified_assets": verified}

    def import_entries(
        self,
        entries: Iterable[ManifestEntry],
        missing_only: bool = True,
        limit: Optional[int] = None,
    ) -> Mapping[str, object]:
        self.database.initialize()
        imported = 0
        rows = 0
        skipped_present = 0
        skipped_imported = 0
        for entry in entries:
            if limit is not None and imported >= limit:
                break
            if self._already_imported(entry):
                skipped_imported += 1
                continue
            exchange = entry.kind[:3].upper()
            if missing_only and self._date_present(exchange, entry.trading_date):
                skipped_present += 1
                continue
            free_bytes = shutil.disk_usage(
                self.database._sqlite_path or entry.path
            ).free
            if free_bytes < self.min_free_bytes:
                return {
                    "imported_files": imported,
                    "rows_written": rows,
                    "skipped_present": skipped_present,
                    "skipped_imported": skipped_imported,
                    "status": "low_disk",
                }
            self._verify(entry)
            self._upsert_assets(
                [
                    {
                        "kind": entry.kind,
                        "trading_date": entry.trading_date,
                        "sha256": entry.sha256,
                        "source_url": entry.url,
                        "local_path": str(entry.path),
                        "size_bytes": entry.size_bytes,
                        "fetched_utc": entry.fetched_utc,
                        "status": "verified_external",
                        "verified_at": utcnow(),
                    }
                ]
            )
            payload = entry.path.read_bytes()
            if entry.kind == "nse_cm":
                records = parse_nse_bhavcopy(payload, entry.trading_date)
            else:
                records = parse_bse_bhavcopy(payload, entry.trading_date)
            if not records:
                raise LocalArchiveError(
                    "{} {} parsed zero rows".format(entry.kind, entry.trading_date)
                )
            for record in records:
                record["source"] = "{}_official_local".format(exchange.lower())
                if record["trading_date"] != entry.trading_date:
                    raise LocalArchiveError(
                        "{} contains a mismatched trading date".format(entry.path)
                    )
            written = self.database.upsert_prices(records, refresh_securities=False)
            self.database.record_checkpoint(
                exchange.lower(),
                "daily_prices",
                entry.trading_date.isoformat(),
                "success",
                entry.trading_date,
                written,
            )
            with self.database.transaction() as connection:
                connection.execute(
                    local_archive_imports.insert().values(
                        kind=entry.kind,
                        trading_date=entry.trading_date,
                        sha256=entry.sha256,
                        source_url=entry.url,
                        local_path=str(entry.path),
                        size_bytes=entry.size_bytes,
                        fetched_utc=entry.fetched_utc,
                        row_count=len(records),
                        imported_at=utcnow(),
                    )
                )
                connection.execute(
                    local_archive_assets.update()
                    .where(
                        local_archive_assets.c.kind == entry.kind,
                        local_archive_assets.c.trading_date == entry.trading_date,
                        local_archive_assets.c.sha256 == entry.sha256,
                    )
                    .values(status="materialized")
                )
            imported += 1
            rows += written
        return {
            "imported_files": imported,
            "rows_written": rows,
            "skipped_present": skipped_present,
            "skipped_imported": skipped_imported,
            "status": "complete",
        }

    def _date_present(self, exchange: str, trading_date: date) -> bool:
        with self.database.engine.connect() as connection:
            return (
                connection.execute(
                    select(daily_prices.c.security_id).where(
                        daily_prices.c.exchange == exchange,
                        daily_prices.c.trading_date == trading_date,
                    ).limit(1)
                ).first()
                is not None
            )

    def _already_imported(self, entry: ManifestEntry) -> bool:
        with self.database.engine.connect() as connection:
            return (
                connection.execute(
                    select(local_archive_imports.c.sha256).where(
                        local_archive_imports.c.kind == entry.kind,
                        local_archive_imports.c.trading_date == entry.trading_date,
                        local_archive_imports.c.sha256 == entry.sha256,
                    )
                ).first()
                is not None
            )

    def _upsert_assets(self, values: List[Dict[str, object]]) -> None:
        with self.database.transaction() as connection:
            imported_hashes = set(
                connection.execute(
                    select(local_archive_imports.c.sha256).where(
                        local_archive_imports.c.sha256.in_(
                            [value["sha256"] for value in values]
                        )
                    )
                ).scalars()
            )
            reconciled = [
                dict(
                    value,
                    status=(
                        "materialized"
                        if value["sha256"] in imported_hashes
                        else value["status"]
                    ),
                )
                for value in values
            ]
            statement = sqlite_insert(local_archive_assets).values(reconciled)
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        local_archive_assets.c.kind,
                        local_archive_assets.c.trading_date,
                        local_archive_assets.c.sha256,
                    ],
                    set_={
                        "source_url": statement.excluded.source_url,
                        "local_path": statement.excluded.local_path,
                        "size_bytes": statement.excluded.size_bytes,
                        "fetched_utc": statement.excluded.fetched_utc,
                        "status": statement.excluded.status,
                        "verified_at": statement.excluded.verified_at,
                    },
                )
            )

    @staticmethod
    def _verify(entry: ManifestEntry) -> None:
        if not entry.path.is_file():
            raise LocalArchiveError("manifested file is missing: {}".format(entry.path))
        if entry.path.stat().st_size != entry.size_bytes:
            raise LocalArchiveError("size mismatch for {}".format(entry.path))
        if _sha256(entry.path) != entry.sha256:
            raise LocalArchiveError("SHA-256 mismatch for {}".format(entry.path))

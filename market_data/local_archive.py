"""Import checksum-manifested official bhavcopies already stored on disk."""

import csv
import gzip
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, replace
from datetime import date, datetime
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
    local_manifest_assets,
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


@dataclass(frozen=True)
class ExternalManifestEntry:
    kind: str
    tag: str
    url: str
    sha256: str
    size_bytes: int
    fetched_utc: str
    path: Path
    status: str


def _payload_digest_and_size(path: Path) -> tuple:
    digest = hashlib.sha256()
    size = 0
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _manifest_path(
    raw_root: Path,
    kind: str,
    tag: str,
    url: str,
    size_bytes: int,
) -> tuple:
    directory = raw_root / kind
    basename = Path(urlparse(url).path).name
    preferred = directory / "{}_{}".format(tag, basename)
    for exact in (preferred, Path(str(preferred) + ".gz")):
        if exact.is_file():
            return exact, "verified_external"
    candidates = sorted(
        {
            path
            for pattern in ("{}.*".format(tag), "{}_*".format(tag))
            for path in directory.glob(pattern)
            if path.is_file()
            and (path.suffix == ".gz" or path.stat().st_size == size_bytes)
        }
    )
    if len(candidates) == 1:
        return candidates[0], "verified_external"
    suspect_directory = raw_root / "{}_suspect".format(kind)
    suspect = suspect_directory / preferred.name
    for exact in (suspect, Path(str(suspect) + ".gz")):
        if exact.is_file():
            return exact, "quarantined"
    return preferred, "missing_local"


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


def load_external_manifest(
    manifest_path: Path,
    raw_root: Path,
    kinds: Optional[Iterable[str]] = None,
) -> List[ExternalManifestEntry]:
    """Load all selected checksum-manifested assets without assuming date tags."""

    selected = set(kinds) if kinds is not None else None
    entries: Dict[tuple, ExternalManifestEntry] = {}
    manifest_path = manifest_path.expanduser().resolve()
    raw_root = raw_root.expanduser().resolve()
    for line_number, line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            kind = str(record["kind"])
            if selected is not None and kind not in selected:
                continue
            tag = str(record["tag"])
            url = str(record["url"])
            size_bytes = int(record["bytes"])
            path, status = _manifest_path(raw_root, kind, tag, url, size_bytes)
            entry = ExternalManifestEntry(
                kind=kind,
                tag=tag,
                url=url,
                sha256=str(record["sha256"]).lower(),
                size_bytes=size_bytes,
                fetched_utc=str(record["fetched_utc"]),
                path=path,
                status=status,
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise LocalArchiveError(
                "invalid external manifest record on line {}".format(line_number)
            ) from error
        key = (entry.kind, entry.tag)
        previous = entries.get(key)
        if previous and previous.sha256 != entry.sha256:
            raise LocalArchiveError(
                "conflicting manifest revisions for {} {}".format(
                    entry.kind, entry.tag
                )
            )
        entries[key] = entry
    return sorted(entries.values(), key=lambda item: (item.kind, item.tag))


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

    def compress_external_entries(
        self, entries: Iterable[ExternalManifestEntry]
    ) -> Mapping[str, int]:
        """Losslessly compress verified payloads with atomic replacement."""

        compressed = 0
        already_compressed = 0
        skipped_unverified = 0
        original_bytes = 0
        compressed_bytes = 0
        for entry in entries:
            if entry.status != "verified_external":
                skipped_unverified += 1
                continue
            self._verify(entry)
            if entry.path.suffix == ".gz":
                already_compressed += 1
                compressed_bytes += entry.path.stat().st_size
                continue
            target = Path(str(entry.path) + ".gz")
            compressed_entry = replace(entry, path=target)
            if target.exists():
                self._verify(compressed_entry)
            else:
                temporary = target.with_name(
                    ".{}.{}.tmp.gz".format(target.name, os.getpid())
                )
                try:
                    with entry.path.open("rb") as source, temporary.open("wb") as raw:
                        with gzip.GzipFile(
                            filename="", mode="wb", fileobj=raw, mtime=0
                        ) as destination:
                            shutil.copyfileobj(source, destination, 1024 * 1024)
                    self._verify(replace(entry, path=temporary))
                    os.replace(str(temporary), str(target))
                finally:
                    if temporary.exists():
                        temporary.unlink()
            original_bytes += entry.path.stat().st_size
            compressed_bytes += target.stat().st_size
            entry.path.unlink()
            compressed += 1
        return {
            "compressed_manifest_assets": compressed,
            "already_compressed_manifest_assets": already_compressed,
            "skipped_unverified_manifest_assets": skipped_unverified,
            "original_bytes": original_bytes,
            "compressed_bytes": compressed_bytes,
        }

    def catalog_external_entries(
        self,
        entries: Iterable[ExternalManifestEntry],
        batch_size: int = 500,
    ) -> Mapping[str, int]:
        """Verify and register arbitrary official manifest assets."""

        self.database.initialize()
        verified = 0
        corrected = 0
        quarantined = 0
        missing = 0
        values = []
        for entry in entries:
            if entry.status != "missing_local":
                self._verify(entry)
            status = entry.status
            observed_tag = None
            correction_note = None
            if status == "quarantined" and entry.kind == "nse_full":
                observed_tag = self._nse_full_observed_date(entry)
                if observed_tag != entry.tag:
                    status = "corrected_external"
                    correction_note = (
                        "Manifest tag {} corrected from DATE1 {}".format(
                            entry.tag, observed_tag
                        )
                    )
            if status == "verified_external":
                verified += 1
            elif status == "corrected_external":
                corrected += 1
            elif status == "quarantined":
                quarantined += 1
            else:
                missing += 1
            values.append(
                {
                    "kind": entry.kind,
                    "tag": entry.tag,
                    "sha256": entry.sha256,
                    "source_url": entry.url,
                    "local_path": str(entry.path),
                    "size_bytes": entry.size_bytes,
                    "fetched_utc": entry.fetched_utc,
                    "status": status,
                    "observed_tag": observed_tag,
                    "correction_note": correction_note,
                    "verified_at": utcnow(),
                }
            )
            if len(values) >= batch_size:
                self._upsert_manifest_assets(values)
                values = []
        if values:
            self._upsert_manifest_assets(values)
        return {
            "verified_manifest_assets": verified,
            "corrected_manifest_assets": corrected,
            "quarantined_manifest_assets": quarantined,
            "missing_manifest_assets": missing,
        }

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
    def _nse_full_observed_date(entry: ExternalManifestEntry) -> str:
        dates = set()
        with entry.path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                value = (row.get("DATE1") or row.get(" DATE1") or "").strip()
                if value:
                    dates.add(
                        datetime.strptime(value, "%d-%b-%Y").date().isoformat()
                    )
        if len(dates) != 1:
            raise LocalArchiveError(
                "{} contains {} distinct DATE1 values".format(
                    entry.path, len(dates)
                )
            )
        return dates.pop()

    def _upsert_manifest_assets(self, values: List[Dict[str, object]]) -> None:
        statement = sqlite_insert(local_manifest_assets).values(values)
        with self.database.transaction() as connection:
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        local_manifest_assets.c.kind,
                        local_manifest_assets.c.tag,
                        local_manifest_assets.c.sha256,
                    ],
                    set_={
                        "source_url": statement.excluded.source_url,
                        "local_path": statement.excluded.local_path,
                        "size_bytes": statement.excluded.size_bytes,
                        "fetched_utc": statement.excluded.fetched_utc,
                        "status": statement.excluded.status,
                        "observed_tag": statement.excluded.observed_tag,
                        "correction_note": statement.excluded.correction_note,
                        "verified_at": statement.excluded.verified_at,
                    },
                )
            )

    @staticmethod
    def _verify(entry: ManifestEntry) -> None:
        if not entry.path.is_file():
            raise LocalArchiveError("manifested file is missing: {}".format(entry.path))
        digest, size = _payload_digest_and_size(entry.path)
        if size != entry.size_bytes:
            raise LocalArchiveError("size mismatch for {}".format(entry.path))
        if digest != entry.sha256:
            raise LocalArchiveError("SHA-256 mismatch for {}".format(entry.path))

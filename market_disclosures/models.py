"""Typed transport and collector results."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Dict, List, Optional


class DisclosureError(RuntimeError):
    """Base error for an exchange source or document."""


class HTTPSourceError(DisclosureError):
    """An exchange returned an unsuccessful response."""

    def __init__(self, url: str, status: int, message: str) -> None:
        super().__init__("%s returned HTTP %s: %s" % (url, status, message))
        self.url = url
        self.status = status


class DocumentFormatError(DisclosureError):
    """A response did not contain the requested document format."""


@dataclass(frozen=True)
class RawDocument:
    """Immutable response bytes and provenance for later archival."""

    url: str
    retrieved_at: datetime
    status: int
    content_type: str
    body: bytes
    sha256: str

    @classmethod
    def from_response(cls, response: Any) -> "RawDocument":
        body = bytes(response.content)
        return cls(
            url=response.url,
            retrieved_at=datetime.now(timezone.utc),
            status=int(response.status_code),
            content_type=response.headers.get("Content-Type", ""),
            body=body,
            sha256=sha256(body).hexdigest(),
        )


@dataclass
class AcquisitionResult:
    """A raw exchange response and its normalized, persistence-ready rows."""

    document: RawDocument
    records: List[Dict[str, Any]] = field(default_factory=list)
    linked_documents: List[RawDocument] = field(default_factory=list)

    def to_dataframe(self) -> Any:
        """Return a pandas DataFrame without making pandas an import-time dependency."""
        import pandas as pd

        return pd.DataFrame(self.records)


def first(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip() not in ("", "-"):
            return value.strip() if isinstance(value, str) else value
    return None


def source_record(
    source: str,
    dataset: str,
    row: Dict[str, Any],
    normalized: Dict[str, Any],
    filing_id: Optional[Any] = None,
    revision_id: Optional[Any] = None,
) -> Dict[str, Any]:
    result = {
        "source": source,
        "dataset": dataset,
        "filing_id": None if filing_id is None else str(filing_id),
        "revision_id": None if revision_id is None else str(revision_id),
    }
    result.update(normalized)
    result["raw"] = dict(row)
    return result

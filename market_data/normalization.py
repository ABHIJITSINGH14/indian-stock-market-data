"""Shared identity normalization for all market-data collectors."""

import hashlib
import re
import unicodedata
from typing import Optional


_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_YAHOO_SUFFIXES = {"NSE": ".NS", "BSE": ".BO"}


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    return " ".join(text.strip().split())


def normalize_isin(value: object) -> Optional[str]:
    isin = re.sub(r"\s+", "", clean_text(value)).upper()
    if not isin or isin in {"N/A", "NA", "NONE", "-", "NULL"}:
        return None
    if not _ISIN_RE.fullmatch(isin):
        return None
    return isin


def normalize_exchange(value: object) -> str:
    exchange = clean_text(value).upper()
    if exchange not in {"NSE", "BSE"}:
        raise ValueError("Unsupported exchange: {!r}".format(value))
    return exchange


def normalize_symbol(
    value: object, exchange: Optional[str] = None, source: Optional[str] = None
) -> str:
    """Normalize casing/spacing without erasing security-significant punctuation.

    Yahoo suffixes are removed only when the caller explicitly identifies Yahoo
    as the source. Official symbols such as ``M&M`` and ``BAJAJ-AUTO`` remain
    distinct.
    """

    symbol = clean_text(value).upper()
    normalized_exchange = normalize_exchange(exchange) if exchange else None
    if source and source.lower() == "yahoo" and normalized_exchange:
        suffix = _YAHOO_SUFFIXES[normalized_exchange]
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
    if not symbol:
        raise ValueError("Symbol cannot be empty")
    return symbol


def canonical_security_id(
    isin: object,
    exchange: object,
    symbol: object,
    series: object = "",
    scrip_code: object = "",
) -> str:
    normalized_isin = normalize_isin(isin)
    if normalized_isin:
        return "isin:{}".format(normalized_isin)

    normalized_exchange = normalize_exchange(exchange)
    normalized_symbol = normalize_symbol(symbol, normalized_exchange)
    discriminator = clean_text(series).upper() or clean_text(scrip_code).upper()
    identity = "{}|{}|{}".format(
        normalized_exchange, normalized_symbol, discriminator
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return "fallback:{}:{}".format(normalized_exchange.lower(), digest)

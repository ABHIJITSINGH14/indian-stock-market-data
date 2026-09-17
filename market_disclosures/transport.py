"""Resilient HTTP transport for official exchange endpoints."""

import json
import random
import time
from datetime import date, timedelta
from typing import Any, Callable, Iterator, Optional, Tuple

import requests

from .models import DocumentFormatError, HTTPSourceError, RawDocument


RETRY_STATUSES = frozenset((401, 403, 429, 500, 502, 503, 504))


def iter_date_windows(
    start: date, end: date, window_days: int = 30
) -> Iterator[Tuple[date, date]]:
    """Split an inclusive range into short, non-overlapping windows."""
    if window_days < 1:
        raise ValueError("window_days must be positive")
    if start > end:
        raise ValueError("start must not be after end")
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=window_days - 1))
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


class ExchangeTransport:
    """Persistent session with throttling, bounded retries, and body validation."""

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        timeout: float = 20.0,
        min_interval: float = 0.25,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        jitter: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.jitter = jitter
        self._sleep = sleep
        self._clock = clock
        self._random = random_value
        self._last_request: Optional[float] = None

    def _throttle(self) -> None:
        if self._last_request is not None:
            remaining = self.min_interval - (self._clock() - self._last_request)
            if remaining > 0:
                self._sleep(remaining)

    def get(
        self, url: str, params: Optional[dict] = None, expected: str = "json"
    ) -> Tuple[RawDocument, Any]:
        response = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise HTTPSourceError(url, 0, str(exc)) from exc
                self._sleep(self._delay(attempt))
                continue
            finally:
                self._last_request = self._clock()

            if response.status_code not in RETRY_STATUSES:
                break
            if attempt >= self.max_retries:
                break
            self._sleep(self._delay(attempt))

        assert response is not None
        document = RawDocument.from_response(response)
        if not 200 <= response.status_code < 300:
            excerpt = response.text[:160].replace("\n", " ")
            raise HTTPSourceError(document.url, document.status, excerpt)
        return document, self._decode(document, expected)

    def _delay(self, attempt: int) -> float:
        return self.backoff_base * (2 ** attempt) + self.jitter * self._random()

    @staticmethod
    def _decode(document: RawDocument, expected: str) -> Any:
        stripped = document.body.lstrip()
        content_type = document.content_type.lower()
        lower_prefix = stripped[:512].lower()
        is_html = (
            b"<html" in lower_prefix
            or b"<!doctype html" in lower_prefix
            or "text/html" in content_type
        )
        if expected == "bytes":
            return document.body
        if is_html:
            raise DocumentFormatError(
                "%s returned HTML instead of %s" % (document.url, expected)
            )
        if expected == "json":
            if not stripped.startswith((b"{", b"[")):
                raise DocumentFormatError(
                    "%s did not return a JSON body" % document.url
                )
            try:
                return json.loads(document.body.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DocumentFormatError(
                    "%s returned invalid JSON" % document.url
                ) from exc
        if expected == "xml":
            if not stripped.startswith(b"<"):
                raise DocumentFormatError(
                    "%s did not return an XML body" % document.url
                )
            return document.body
        raise ValueError("unsupported expected document type: %s" % expected)

    def close(self) -> None:
        self.session.close()

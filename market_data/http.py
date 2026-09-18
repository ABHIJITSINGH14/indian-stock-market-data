"""Official exchange HTTP client with throttling and content validation."""

import threading
import time
from urllib.parse import urlparse
from typing import Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class SourceResponseError(RuntimeError):
    pass


class HostCircuitOpen(SourceResponseError):
    pass


class HostRateLimiter:
    """Serialize request starts per host without serializing response downloads."""

    def __init__(self, delay: float, failure_threshold: int = 3, cooldown: float = 300.0):
        self.delay = max(0.0, delay)
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self._lock = threading.Lock()
        self._next_request = {}
        self._forbidden = {}
        self._blocked_until = {}

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        with self._lock:
            now = time.monotonic()
            blocked_until = self._blocked_until.get(host, 0.0)
            if blocked_until > now:
                raise HostCircuitOpen(
                    "{} circuit open after repeated HTTP 403 responses; retry after cooldown".format(
                        host
                    )
                )
            wait_for = max(0.0, self._next_request.get(host, now) - now)
            if wait_for:
                time.sleep(wait_for)
            self._next_request[host] = time.monotonic() + self.delay

    def response(self, url: str, status_code: int) -> None:
        host = urlparse(url).netloc.lower()
        with self._lock:
            if status_code == 403:
                failures = self._forbidden.get(host, 0) + 1
                self._forbidden[host] = failures
                if failures >= self.failure_threshold:
                    self._blocked_until[host] = time.monotonic() + self.cooldown
            elif status_code < 400:
                self._forbidden[host] = 0


class ExchangeHTTPClient:
    def __init__(
        self,
        timeout: float = 30.0,
        delay: float = 0.5,
        retries: int = 3,
        session: Optional[requests.Session] = None,
        rate_limiter: Optional[HostRateLimiter] = None,
    ):
        self.timeout = timeout
        self.delay = delay
        self.session = session or requests.Session()
        self.rate_limiter = rate_limiter
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
        self._bootstrapped = set()

    def bootstrap(self, source: str, base_url: str) -> None:
        if source in self._bootstrapped:
            return
        self._wait(base_url)
        response = self.session.get(
            base_url, headers=self.headers, timeout=self.timeout
        )
        if self.rate_limiter and response.status_code == 403:
            self.rate_limiter.response(base_url, response.status_code)
        response.raise_for_status()
        if self.rate_limiter:
            self.rate_limiter.response(base_url, response.status_code)
        self._bootstrapped.add(source)

    def get(
        self,
        url: str,
        source: str,
        base_url: Optional[str] = None,
        referer: Optional[str] = None,
        params: Optional[Dict[str, object]] = None,
        expected: Optional[str] = None,
    ) -> requests.Response:
        if base_url:
            self.bootstrap(source, base_url)
        self._wait(url)
        headers = dict(self.headers)
        if referer or base_url:
            headers["Referer"] = referer or base_url
        response = self.session.get(
            url, params=params, headers=headers, timeout=self.timeout
        )
        if self.rate_limiter and response.status_code == 403:
            self.rate_limiter.response(url, response.status_code)
        response.raise_for_status()
        self._validate(response, expected)
        if self.rate_limiter:
            self.rate_limiter.response(url, response.status_code)
        return response

    def _wait(self, url: str) -> None:
        if self.rate_limiter:
            self.rate_limiter.wait(url)
        elif self.delay:
            time.sleep(self.delay)

    @staticmethod
    def _validate(response: requests.Response, expected: Optional[str]) -> None:
        content = response.content
        if not content:
            raise SourceResponseError("Official source returned an empty response")
        content_type = response.headers.get("Content-Type", "").lower()
        prefix = content[:512].lstrip().lower()
        if prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html"):
            raise SourceResponseError(
                "Official source returned HTML instead of market data"
            )
        if expected == "zip" and not content.startswith(b"PK"):
            raise SourceResponseError(
                "Official source did not return a valid ZIP archive ({})".format(
                    content_type or "unknown content type"
                )
            )
        if expected == "csv" and b"," not in content[:4096]:
            raise SourceResponseError("Official source response is not CSV data")
        if expected == "json":
            try:
                response.json()
            except ValueError as exc:
                raise SourceResponseError(
                    "Official source response is not valid JSON"
                ) from exc

    def close(self) -> None:
        self.session.close()

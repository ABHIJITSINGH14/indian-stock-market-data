"""Official exchange HTTP client with throttling and content validation."""

import time
from typing import Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class SourceResponseError(RuntimeError):
    pass


class ExchangeHTTPClient:
    def __init__(
        self,
        timeout: float = 30.0,
        delay: float = 0.5,
        retries: int = 3,
        session: Optional[requests.Session] = None,
    ):
        self.timeout = timeout
        self.delay = delay
        self.session = session or requests.Session()
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
        response = self.session.get(
            base_url, headers=self.headers, timeout=self.timeout
        )
        response.raise_for_status()
        self._bootstrapped.add(source)

    def get(
        self,
        url: str,
        source: str,
        base_url: Optional[str] = None,
        params: Optional[Dict[str, object]] = None,
        expected: Optional[str] = None,
    ) -> requests.Response:
        if base_url:
            self.bootstrap(source, base_url)
        if self.delay:
            time.sleep(self.delay)
        headers = dict(self.headers)
        if base_url:
            headers["Referer"] = base_url
        response = self.session.get(
            url, params=params, headers=headers, timeout=self.timeout
        )
        response.raise_for_status()
        self._validate(response, expected)
        return response

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

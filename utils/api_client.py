import time
import hashlib
from urllib.parse import urlsplit, urlunsplit

import requests

from config.config import API_RETRY_ATTEMPTS, API_RETRY_DELAY, API_TIMEOUT, REQUEST_DELAY
from utils.logger import setup_logger

logger = setup_logger(__name__)


class APIClient:
    """Bounded requests; do not retry access denials, rate limits, or TLS errors."""

    def __init__(self):
        self.session = requests.Session()
        self.last_request_metadata = {}
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

    def get(self, url, params=None, retry=True):
        # Do not retain query strings, cookies, response bodies or exception text.
        parsed = urlsplit(url)
        source = urlunsplit((parsed.scheme, parsed.hostname or '', parsed.path, '', ''))
        self.last_request_metadata = {'source_url': source, 'status': 'not_completed'}
        attempts = max(1, API_RETRY_ATTEMPTS) if retry else 1
        for attempt in range(1, attempts + 1):
            self.last_request_metadata = {'source_url': source, 'status': 'not_completed',
                                          'attempts': attempt}
            try:
                time.sleep(REQUEST_DELAY)
                response = self.session.get(
                    url, params=params, headers=self.headers, timeout=API_TIMEOUT
                )
                self.last_request_metadata = {
                    'source_url': source, 'status': 'response_received', 'attempts': attempt,
                    'status_code': response.status_code,
                    'content_type': response.headers.get('Content-Type', '').split(';')[0].lower(),
                    'body_bytes': len(response.content),
                    'body_sha256': hashlib.sha256(response.content).hexdigest(),
                    'redirect_statuses': [item.status_code for item in response.history],
                }
                response.raise_for_status()
                logger.info("HTTP %s received: %s", response.status_code, url)
                return response
            except requests.exceptions.RequestException as exc:
                response = getattr(exc, 'response', None)
                status = response.status_code if response is not None else None
                # A 429 is not permission to send another immediate request.
                # Leave TLS verification enabled; retrying cannot repair a TLS failure.
                terminal = isinstance(exc, requests.exceptions.SSLError) or (
                    status is not None and 400 <= status < 500
                )
                self.last_request_metadata.update(
                    status='request_failed', attempts=attempt,
                    error_type=type(exc).__name__,
                    failure_category=('tls_error' if isinstance(exc, requests.exceptions.SSLError)
                                      else 'http_error' if status is not None else 'transport_error'),
                )
                if terminal or attempt == attempts:
                    logger.error("Failed to fetch %s after %s attempt(s): %s", url, attempt, exc)
                    raise
                logger.warning(
                    "Attempt %s failed for %s: %s. Retrying in %ss...",
                    attempt, url, exc, API_RETRY_DELAY,
                )
                time.sleep(API_RETRY_DELAY)

    def download_file(self, url, save_path):
        try:
            response = self.get(url, retry=True)
            if response is not None:
                with open(save_path, 'wb') as file:
                    file.write(response.content)
                logger.info("File saved to %s", save_path)
                return True
        except Exception as exc:
            logger.error("Error downloading file: %s", exc)
        return False

    def close(self):
        self.session.close()

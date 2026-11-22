"""Bounded retry for read-only GitHub requests. Never wrap a write with this."""
import socket
import ssl
import time
from urllib.error import HTTPError, URLError

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DELAYS = (1, 2)


def is_transient(error):
    """Network blips and server-side throttling; rules, permissions and 404 are never transient."""
    if isinstance(error, HTTPError):
        headers = error.headers
        return error.code in RETRYABLE_STATUS or (
            error.code == 403 and headers is not None and headers.get('Retry-After') is not None)
    return isinstance(error, (URLError, TimeoutError, socket.timeout, ConnectionError, ssl.SSLError))


def retry(call, *, attempts=3):
    """Run an idempotent read at most `attempts` times, waiting 1s then 2s between tries."""
    for attempt in range(attempts):
        try:
            return call()
        except Exception as error:
            if attempt == attempts - 1 or not is_transient(error):
                raise
            time.sleep(DELAYS[min(attempt, len(DELAYS) - 1)])

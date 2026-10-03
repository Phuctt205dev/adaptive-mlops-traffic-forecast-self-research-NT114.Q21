import time
from collections import defaultdict, deque

from backend.app.core.errors import ApplicationError


_requests: dict[str, deque[float]] = defaultdict(deque)


def check_rate_limit(key: str, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    bucket = _requests[key]
    while bucket and bucket[0] <= now - window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        raise ApplicationError(
            "rate_limit_exceeded",
            "Too many requests. Please try again later.",
            429,
        )
    bucket.append(now)

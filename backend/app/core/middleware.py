import logging
import time

from fastapi import FastAPI, Request

from backend.app.core import metrics


logger = logging.getLogger("traffic_api.requests")


def register_operational_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def add_security_headers_and_log_requests(request: Request, call_next):
        started = time.perf_counter()
        metrics.IN_PROGRESS.inc()
        status_code = 500   # an exception escaping the app is answered with 500 by the server
        try:
            response = await call_next(request)
            status_code = response.status_code
        finally:
            seconds = time.perf_counter() - started
            metrics.IN_PROGRESS.dec()
            metrics.observe(request.scope, request.headers, request.method, status_code, seconds, request.app)
        logger.info(
            "%s %s -> %s %.2fms",
            request.method,
            request.url.path,
            response.status_code,
            seconds * 1000,
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        return response

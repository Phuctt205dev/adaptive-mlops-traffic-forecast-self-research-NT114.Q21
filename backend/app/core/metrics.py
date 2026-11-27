"""Request metrics of the API for Prometheus: count, latency and requests in progress.

The metrics are served by a small HTTP server on a separate port (TRAFFIC_METRICS_PORT, default 9464).
The public path (ngrok -> Nginx -> NodePort 30080) only reaches port 8000, so /metrics is never public;
Prometheus scrapes the pod port directly (deployment/observability/api-podmonitor.yaml).

Labels stay bounded: the route template (never the raw path), the status code, the HTTP method and
where the request came from (`source`):
- probe:   kubelet health checks,
- web:     the website through ngrok (header sent by frontend/src/api.js, or an ngrok host),
- proxy:   other requests through the Nginx proxy (for example a load test over the private network),
- cluster: calls inside the cluster (Airflow, workers, Prometheus).
"""
import logging
import os
import re

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server
from prometheus_client import PlatformCollector, ProcessCollector


logger = logging.getLogger("traffic_api.metrics")
REGISTRY = CollectorRegistry(auto_describe=True)
ProcessCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)

# Seconds. Covers fast reads (tens of ms) up to slow model-quality reports.
BUCKETS = (0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
NGROK_SUFFIXES = (".ngrok-free.app", ".ngrok-free.dev", ".ngrok.app", ".ngrok.dev", ".ngrok.io")
DEFAULT_PORT = 9464

REQUESTS = Counter("traffic_api_requests", "HTTP requests handled by the API.",
                   ("route", "method", "status", "source"), registry=REGISTRY)
LATENCY = Histogram("traffic_api_request_duration_seconds", "Time to produce the HTTP response.",
                    ("route", "method", "source"), buckets=BUCKETS, registry=REGISTRY)
IN_PROGRESS = Gauge("traffic_api_requests_in_progress", "HTTP requests being handled now.", registry=REGISTRY)


def request_source(headers) -> str:
    """Classify where a request came from, using headers only (no client address is stored)."""
    if headers.get("user-agent", "").startswith("kube-probe/"):
        return "probe"
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(":")[0].lower()
    if "ngrok-skip-browser-warning" in headers or host.endswith(NGROK_SUFFIXES):
        return "web"
    if "x-forwarded-for" in headers:
        return "proxy"
    return "cluster"


def method_label(method: str) -> str:
    method = method.upper()
    return method if method in METHODS else "OTHER"


_TEMPLATES = {}   # id(app) -> [(compiled regex, full template)], built once from the app's OpenAPI paths


def _templates(app):
    key = id(app)
    if key not in _TEMPLATES:
        compiled = []
        try:
            for template in app.openapi().get("paths", {}):
                pattern = re.sub(r"\{[^}/]+:path\}", ".+", re.escape(template).replace(r"\{", "{").replace(r"\}", "}"))
                pattern = re.sub(r"\{[^}/]+\}", "[^/]+", pattern)
                compiled.append((re.compile(pattern + r"\Z"), template))
        except Exception as error:   # labels must never break a request
            logger.warning("Route templates unavailable for metrics: %s", type(error).__name__)
        # Static templates before parameterised ones, so /regions/lookup wins over /regions/{id}.
        compiled.sort(key=lambda item: (item[1].count("{"), -len(item[1])))
        _TEMPLATES[key] = compiled
    return _TEMPLATES[key]


def route_label(scope, app=None) -> str:
    """The full route template, e.g. /api/v1/regions/{region_id}/forecast-dashboard.

    FastAPI keeps included routers nested, so scope["route"].path is only relative to the innermost
    router (it can even be ""). The full template therefore comes from the app's OpenAPI paths. Routes
    hidden from OpenAPI fall back to the relative template; a request answered before routing
    (PredictionGate's 503 busy) still matches by path. A raw path is never used as a label.
    """
    if app is not None and scope.get("type") == "http":
        for pattern, template in _templates(app):
            if pattern.match(scope.get("path", "")):
                return template
    path = getattr(scope.get("route"), "path", None)
    if isinstance(path, str) and path:
        return path
    if app is not None and scope.get("type") == "http":
        from starlette.routing import Match
        for route in getattr(getattr(app, "router", None), "routes", ()):
            try:
                match, _ = route.matches(scope)
            except Exception:   # a route that cannot inspect this scope is skipped
                continue
            if match is not Match.NONE and isinstance(getattr(route, "path", None), str):
                return route.path
    return "unmatched"


def observe(scope, headers, method: str, status_code: int, seconds: float, app=None) -> None:
    route, source, method = route_label(scope, app), request_source(headers), method_label(method)
    REQUESTS.labels(route, method, str(status_code), source).inc()
    LATENCY.labels(route, method, source).observe(seconds)


def start_metrics_server():
    """Start the private metrics endpoint; returns the port, or None when disabled or unavailable."""
    port = int(os.environ.get("TRAFFIC_METRICS_PORT", DEFAULT_PORT))
    if port <= 0:
        return None
    try:
        start_http_server(port, addr="0.0.0.0", registry=REGISTRY)
    except OSError as error:   # metrics must never stop the API from serving
        logger.warning("Metrics endpoint not started on port %s: %s", port, error)
        return None
    return port

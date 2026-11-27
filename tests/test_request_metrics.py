"""Request metrics for Prometheus (E4 step 6.3): labels, sources, failures and the private port."""
import os
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core import metrics
from backend.app.core.middleware import register_operational_middleware


def sample(name, **labels):
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def build():
    app = FastAPI()
    register_operational_middleware(app)

    @app.get("/metrics-test/items/{item_id}")
    def item(item_id: int):
        return {"id": item_id}

    @app.get("/metrics-test/boom")
    def boom():
        raise RuntimeError("synthetic failure")

    return app


class RequestSourceTests(unittest.TestCase):
    def test_kubelet_probe(self):
        self.assertEqual(metrics.request_source({"user-agent": "kube-probe/1.36"}), "probe")

    def test_web_through_ngrok_header_or_host(self):
        self.assertEqual(metrics.request_source({"ngrok-skip-browser-warning": "true", "x-forwarded-for": "1.2.3.4"}), "web")
        self.assertEqual(metrics.request_source({"host": "demo.ngrok-free.app", "x-forwarded-for": "1.2.3.4"}), "web")
        self.assertEqual(metrics.request_source({"x-forwarded-host": "demo.ngrok-free.dev:443"}), "web")

    def test_direct_proxy_and_cluster(self):
        self.assertEqual(metrics.request_source({"host": "node:8080", "x-forwarded-for": "100.64.0.1"}), "proxy")
        self.assertEqual(metrics.request_source({"host": "traffic-api-training:8000"}), "cluster")


class MiddlewareMetricsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(build(), raise_server_exceptions=False)

    def test_route_template_not_raw_path(self):
        before = sample("traffic_api_requests_total", route="/metrics-test/items/{item_id}", method="GET",
                        status="200", source="cluster")
        for item_id in (1, 2, 3):
            self.assertEqual(self.client.get(f"/metrics-test/items/{item_id}").status_code, 200)
        after = sample("traffic_api_requests_total", route="/metrics-test/items/{item_id}", method="GET",
                       status="200", source="cluster")
        self.assertEqual(after - before, 3)
        self.assertGreaterEqual(sample("traffic_api_request_duration_seconds_count",
                                       route="/metrics-test/items/{item_id}", method="GET", source="cluster"), 3)
        self.assertEqual(sample("traffic_api_requests_total", route="/metrics-test/items/1", method="GET",
                                status="200", source="cluster"), 0)

    def test_unmatched_and_validation_status(self):
        before = sample("traffic_api_requests_total", route="unmatched", method="GET", status="404", source="web")
        self.client.get("/no-such-path/12345", headers={"ngrok-skip-browser-warning": "true"})
        self.assertEqual(sample("traffic_api_requests_total", route="unmatched", method="GET", status="404",
                                source="web") - before, 1)
        self.assertEqual(self.client.get("/metrics-test/items/not-a-number").status_code, 422)
        self.assertEqual(sample("traffic_api_requests_total", route="/metrics-test/items/{item_id}", method="GET",
                                status="422", source="cluster"), 1)

    def test_unhandled_error_is_counted_as_500(self):
        before = sample("traffic_api_requests_total", route="/metrics-test/boom", method="GET", status="500", source="cluster")
        self.assertEqual(self.client.get("/metrics-test/boom").status_code, 500)
        self.assertEqual(sample("traffic_api_requests_total", route="/metrics-test/boom", method="GET", status="500",
                                source="cluster") - before, 1)
        self.assertEqual(sample("traffic_api_requests_in_progress"), 0)

    def test_reply_before_routing_keeps_the_route_template(self):
        """PredictionGate answers 503 before the router runs; the route must still be named."""
        from starlette.middleware import Middleware
        from starlette.responses import JSONResponse

        class Busy:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http" and scope["path"].startswith("/metrics-test/items/"):
                    await JSONResponse({"error": "busy"}, status_code=503)(scope, receive, send)
                    return
                await self.app(scope, receive, send)

        app = build()
        app.user_middleware.append(Middleware(Busy))   # innermost, like serving_runtime.build_app
        client = TestClient(app)
        before = sample("traffic_api_requests_total", route="/metrics-test/items/{item_id}", method="GET",
                        status="503", source="cluster")
        self.assertEqual(client.get("/metrics-test/items/7").status_code, 503)
        self.assertEqual(sample("traffic_api_requests_total", route="/metrics-test/items/{item_id}", method="GET",
                                status="503", source="cluster") - before, 1)

    def test_unknown_method_is_folded(self):
        self.assertEqual(metrics.method_label("PROPFIND"), "OTHER")
        self.assertEqual(metrics.method_label("post"), "POST")

    def test_metrics_are_not_served_on_the_public_app(self):
        self.assertEqual(self.client.get("/metrics").status_code, 404)


class NestedRouterLabelTests(unittest.TestCase):
    """FastAPI keeps included routers nested: scope["route"].path is relative to the innermost router
    (found live on 27/11: '/training-monitor/capabilities' for /api/v1/admin/..., '' for /api/v1/admin/regions)."""

    def setUp(self):
        from fastapi import APIRouter
        app = FastAPI()
        register_operational_middleware(app)
        admin = APIRouter(prefix="/admin")
        regions = APIRouter(prefix="/regions")

        @regions.get("")
        def list_regions():
            return []

        @regions.get("/{region_id}/drift-checks")
        def checks(region_id: str):
            return []

        scoped = APIRouter(prefix="/regions/{region_id}")

        @scoped.get("/model-quality")
        def quality(region_id: str):
            return {}

        @scoped.get("/hidden", include_in_schema=False)
        def hidden(region_id: str):
            return {}

        admin.include_router(regions)
        admin.include_router(scoped, prefix="/scoped")
        api = APIRouter()
        api.include_router(admin)
        app.include_router(api, prefix="/api/v1")
        self.client = TestClient(app)

    def labelled(self, path):
        before = {s.labels["route"]: s.value for m in metrics.REGISTRY.collect() if m.name == "traffic_api_requests"
                  for s in m.samples if s.name == "traffic_api_requests_total"}
        self.client.get(path)
        after = {s.labels["route"]: s.value for m in metrics.REGISTRY.collect() if m.name == "traffic_api_requests"
                 for s in m.samples if s.name == "traffic_api_requests_total"}
        return [route for route, value in after.items() if value > before.get(route, 0)]

    def test_full_template_for_nested_routers(self):
        self.assertEqual(self.labelled("/api/v1/admin/regions"), ["/api/v1/admin/regions"])
        self.assertEqual(self.labelled("/api/v1/admin/regions/abc-123/drift-checks"),
                         ["/api/v1/admin/regions/{region_id}/drift-checks"])

    def test_parameter_in_router_prefix_never_leaks_the_value(self):
        self.assertEqual(self.labelled("/api/v1/admin/scoped/regions/secret-id-42/model-quality"),
                         ["/api/v1/admin/scoped/regions/{region_id}/model-quality"])
        hidden = self.labelled("/api/v1/admin/scoped/regions/secret-id-42/hidden")
        self.assertEqual(len(hidden), 1)
        self.assertNotIn("secret-id-42", hidden[0])


class RolePackagingTests(unittest.TestCase):
    def test_api_image_carries_metrics_and_dependency(self):
        from pathlib import Path
        from scripts.cicd import roles_release
        root = Path(__file__).resolve().parents[1]
        self.assertIn("backend/app/core/metrics.py", roles_release.closure(root, "api"))
        self.assertNotIn("backend/app/core/metrics.py", roles_release.closure(root, "sync"))   # sync has no web deps
        web = (root / "requirements/roles/web.txt").read_text(encoding="utf-8")
        constraints = (root / "requirements/roles/constraints.txt").read_text(encoding="utf-8")
        self.assertIn("prometheus-client==0.26.0", web)
        self.assertIn("prometheus-client==0.26.0", constraints)

    def test_runtime_starts_metrics_only_for_api(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "deployment/rke2/runtime/role_runtime.py").read_text(encoding="utf-8")
        self.assertIn("if document['role'] == 'api':", source)
        self.assertIn("start_metrics_server()", source)


class MetricsServerTests(unittest.TestCase):
    def test_default_port_and_disable(self):
        with mock.patch.object(metrics, "start_http_server") as start, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRAFFIC_METRICS_PORT", None)
            self.assertEqual(metrics.start_metrics_server(), 9464)
            start.assert_called_once_with(9464, addr="0.0.0.0", registry=metrics.REGISTRY)
        with mock.patch.object(metrics, "start_http_server") as start, \
                mock.patch.dict(os.environ, {"TRAFFIC_METRICS_PORT": "0"}):
            self.assertIsNone(metrics.start_metrics_server())
            start.assert_not_called()

    def test_server_failure_does_not_stop_serving(self):
        with mock.patch.object(metrics, "start_http_server", side_effect=OSError("port in use")), \
                mock.patch.dict(os.environ, {"TRAFFIC_METRICS_PORT": "9464"}):
            self.assertIsNone(metrics.start_metrics_server())

    def test_exposition_contains_request_metrics(self):
        from prometheus_client import generate_latest
        metrics.observe({}, {}, "GET", 200, 0.01)
        text = generate_latest(metrics.REGISTRY).decode()
        self.assertIn("traffic_api_requests_total", text)
        self.assertIn("traffic_api_request_duration_seconds_bucket", text)
        if os.path.exists("/proc/self/stat"):   # the process collector reads /proc (Linux pods and CI)
            self.assertIn("process_resident_memory_bytes", text)


if __name__ == "__main__":
    unittest.main()

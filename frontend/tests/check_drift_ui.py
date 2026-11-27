"""Built UI with synthetic APIs only: Drift page with the traffic-PSI decision and scheduled retrain.

Never calls RKE2, Kaggle or GitHub. Run `npm run build` in frontend/ first.
Usage: python frontend/tests/check_drift_ui.py [SCREENSHOT_DIR]
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
BASE = "/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/"
REGION, MODEL = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
WEATHER = ["temperature", "dew_point", "wind_direction", "humidity", "wind_speed", "clouds_all", "rain_p_h",
           "snow_p_h", "air_pollution_index", "visibility_in_miles", "is_holiday", "weather_type", "weather_description"]


def check(number, *, status, traffic=None, reason=None, skip=None, age=12.4, drifted=6, created="2026-11-27T00:00:00Z"):
    features = {name: {"type": "numeric", "metric": "psi", "value": 0.35 if i < drifted else 0.05,
                       "threshold": 0.2, "drift": i < drifted} for i, name in enumerate(WEATHER)}
    summary = {"feature_count": 13, "drifted_feature_count": drifted, "min_drifted_features": 2,
               "drift_detected": drifted >= 2, "check_method": "auto"}
    report = {"summary": summary, "features": features}
    if traffic is not None:   # checks saved after the change
        summary.update(weather_drift_detected=drifted >= 2, decision_signal="traffic_volume_psi", traffic_psi=traffic,
                       traffic_psi_threshold=1.0, drift_detected=traffic > 1.0, model_data_age_days=age,
                       scheduled_retrain_days=28)
        report["traffic"] = {"metric": "psi", "value": traffic, "threshold": 1.0, "drift": traffic > 1.0}
    if reason:
        report["retrain_reason"] = reason
    if skip:
        report["retrain_skip_reason"] = skip
    return {"id": f"check-{number}", "region_id": REGION, "dataset_id": "d", "active_model_version_id": MODEL,
            "reference_start_at": "2025-12-02T00:00:00Z", "reference_end_at": "2026-01-01T00:00:00Z",
            "current_start_at": "2026-11-20T00:00:00Z", "current_end_at": "2026-11-27T00:00:00Z",
            "status": status, "drift_detected": summary["drift_detected"], "drifted_feature_count": drifted,
            "feature_count": 13, "feature_drift_json": report,
            "triggered_training_run_id": f"run-{number}" if status == "retrain_triggered" else None,
            "error_message": None, "created_at": created, "method": "auto"}


CHECKS = [
    check(1, status="retrain_triggered", traffic=0.35, reason="scheduled", age=29.0, created="2026-11-27T00:00:00Z"),
    check(2, status="retrain_triggered", traffic=2.5, reason="traffic_drift", created="2026-11-25T00:00:00Z"),
    check(3, status="drift_detected", traffic=1.8, skip="candidate_pending", created="2026-11-23T00:00:00Z"),
    check(4, status="stable", traffic=0.12, drifted=5, created="2026-11-21T00:00:00Z"),
    check(5, status="drift_detected", drifted=6, created="2026-11-12T00:00:00Z"),   # saved before the change
]


def main():
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    errors, cases = [], []

    class Handler(SimpleHTTPRequestHandler):
        def translate_path(self, path):
            return super().translate_path("/" + path[len(BASE):] if path.startswith(BASE) else path)
        def log_message(self, *args):
            pass

    def reply(route, body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def respond(route):
        request, url = route.request, urlparse(route.request.url)
        if "/api/v1/" not in url.path:
            route.continue_() if url.hostname == "127.0.0.1" else route.abort()
            return
        if request.method != "GET":
            reply(route, {"error": {"message": "Unexpected synthetic action"}}, 500)
            return
        if url.path.endswith("/drift-checks"):
            body = {"region_id": REGION, "latest": CHECKS[0], "items": CHECKS, "total": len(CHECKS),
                    "active_model_feature_usage": {"model_version_id": MODEL, "input_features": [], "features": {}}}
        elif url.path.endswith("/drift-settings"):
            body = {"region_id": REGION, "auto_retrain": True}
        elif url.path.endswith("/prediction-window"):
            body = {}
        elif url.path.endswith("/auth/me"):
            body = {"id": "admin", "display_name": "Admin", "email": "admin@example.test", "role": "admin", "is_active": True}
        elif url.path.endswith("/regions"):
            body = {"items": [{"id": REGION, "name": "Synthetic region", "timezone": "Asia/Ho_Chi_Minh", "latitude": 10,
                               "longitude": 106, "is_active": True, "active_model_version_id": MODEL}]}
        elif url.path.endswith("/capabilities"):
            body = {"cloud_training": True, "local_training": True, "local_unavailable_reason": ""}
        elif url.path.endswith("/training-monitor"):
            body = {"items": [], "enabled": True}
        else:
            body = {"items": []}
        reply(route, body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "frontend/dist")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1050})
            context.add_init_script("if(location.hostname==='127.0.0.1')localStorage.setItem('traffic_token','synthetic-token')")
            context.route("**/*", respond)
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{server.server_port}{BASE}")
            page.get_by_role("button", name="Drift", exact=True).click()
            expect(page.locator(".topbar h1")).to_have_text("Drift")
            summary = page.locator(".drift-summary")
            status = page.locator(".drift-status-card")
            history = page.locator("table", has=page.locator("th", has_text="Method"))   # Drift History only
            rows = history.locator("tbody tr")
            expect(rows).to_have_count(5)

            expect(status).to_contain_text("Scheduled retrain")
            expect(status).to_have_class("drift-status-card drift-status-scheduled")
            expect(summary).to_contain_text("Traffic change (PSI)")
            expect(summary).to_contain_text("0.35 / 1.00")
            expect(summary).to_contain_text("Within threshold")
            expect(summary).to_contain_text("Weather features (reference)")
            expect(page.get_by_text("Retrain triggered (scheduled, model data older than 28 days): run-1")).to_be_visible()
            expect(page.get_by_text("Weather and Holiday Scores (reference)")).to_be_visible()
            note = page.locator(".drift-threshold-note")
            expect(note).to_contain_text("PSI of traffic_volume above 1.00")
            expect(note).to_contain_text("Model data is 29 days old at this check; scheduled retrain at 28 days.")
            cases.append("scheduled retrain: blue status, traffic PSI card, weather marked as reference")

            header = history.locator("thead")
            expect(header).to_contain_text("Traffic PSI")
            expect(rows.nth(0)).to_contain_text("0.35")
            expect(rows.nth(0)).to_contain_text("scheduled")
            expect(rows.nth(1)).to_contain_text("traffic")
            expect(rows.nth(4)).to_contain_text("-")
            cases.append("history shows traffic PSI and the retrain reason")

            rows.nth(1).click()
            expect(status).to_contain_text("Retrain triggered")
            expect(status).to_have_class("drift-status-card drift-status-retrain_triggered")
            expect(page.locator(".drift-traffic-card")).to_have_class("drift-traffic-card drift-traffic-changed")
            expect(summary).to_contain_text("Above threshold")
            expect(page.get_by_text("Retrain triggered (traffic changed): run-2")).to_be_visible()
            cases.append("traffic drift retrain keeps the warning colour")

            rows.nth(2).click()
            expect(status).to_contain_text("Drift detected")
            expect(page.get_by_text("Retrain skipped: A model retrained from the active model is waiting to be activated")).to_be_visible()
            cases.append("pending candidate is explained in words")

            rows.nth(3).click()
            expect(status).to_contain_text("Stable")
            expect(summary).to_contain_text("5/13")
            cases.append("weather drift alone shows Stable")

            rows.nth(4).click()
            expect(page.locator(".drift-traffic-card")).to_have_count(0)
            expect(page.get_by_text("Feature Scores", exact=True)).to_be_visible()
            expect(note).to_contain_text("Drift is detected when at least 2 features exceed their thresholds.")
            expect(summary).to_contain_text("Drifted features")
            cases.append("checks saved before the change keep the old view")

            rows.nth(0).click()
            for width in (1440, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                assert overflow <= 1, f"horizontal scroll at {width}px: {overflow}"
                spill = page.evaluate("[...document.querySelectorAll('.drift-summary > div')]"
                                      ".map(c => c.scrollWidth - c.clientWidth).reduce((a, b) => Math.max(a, b), 0)")
                assert spill <= 1, f"a summary card spills {spill}px at {width}px"
            cases.append("no horizontal scroll at 1440, 390 and 320 px")
            if shots:
                shots.mkdir(parents=True, exist_ok=True)
                # Tall enough that the whole page fits without scrolling under the sticky top bar.
                page.set_viewport_size({"width": 1440, "height": 2600})
                page.evaluate("window.scrollTo(0, 0)")
                page.locator(".drift-layout").screenshot(path=str(shots / "E3-8-drift-page-after.png"))
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
    for case in cases:
        print("PASS", case)
    print(f"{len(cases)} cases passed")


if __name__ == "__main__":
    main()

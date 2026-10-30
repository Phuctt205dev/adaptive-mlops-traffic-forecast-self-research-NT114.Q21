"""Check the built admin page against synthetic API responses; no live API writes."""
import copy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
BASE = "/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/"
OUTPUT = ROOT / "results/feature-importance/five-model-ui"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    state = {"scenario": "ready", "role": "admin", "active": "model-a"}
    calls, errors, mutations = [], [], []
    active_rows = [{"label": label, "share_percent": share, "mean_abs_shap": share, "features": [name]}
                   for label, name, share in (("Weather conditions", "weather_type_Rain", 40),
                       ("Time of day", "hour", 30), ("Temperature", "temperature", 30))]
    average_rows = [{"label": "Temperature", "share_percent": 40, "features": ["temperature"], "reports_present": 3},
                    {"label": "Time of day", "share_percent": 20, "features": ["hour"], "reports_present": 2}]
    average_rows += [{"label": f"Input {index}", "share_percent": 4, "features": [f"input_{index}"], "reports_present": 1}
                     for index in range(10)]

    class Handler(SimpleHTTPRequestHandler):
        def translate_path(self, path):
            if path.startswith(BASE):
                path = "/" + path[len(BASE):]
            return super().translate_path(path)

        def log_message(self, *args):
            pass

    def respond(route):
        parsed = urlparse(route.request.url)
        path = parsed.path
        if "/api/v1/" not in path:
            route.continue_() if parsed.hostname == "127.0.0.1" else route.abort()
            return
        if route.request.method not in {"GET", "OPTIONS"}:
            mutations.append(path)
        status = 200
        if path.endswith("/auth/me"):
            body = {"id": "user", "email": "student@example.test", "role": state["role"], "is_active": True}
        elif path.endswith("/regions"):
            body = {"items": [{"id": "region-a", "name": "Demo Region", "timezone": "Asia/Ho_Chi_Minh",
                                "active_model_version_id": state["active"], "is_active": True},
                               {"id": "region-b", "name": "No Active Region", "timezone": "Asia/Ho_Chi_Minh",
                                "active_model_version_id": None, "is_active": True},
                               {"id": "region-c", "name": "Empty Region", "timezone": "Asia/Ho_Chi_Minh",
                                "active_model_version_id": None, "is_active": True}]}
        elif path.endswith("/model-versions"):
            body = {"items": [{"id": key, "version": f"model_v{index}", "variant": variant,
                               "status": "active" if state["active"] == key else "candidate",
                               "cv_mean_mae": 180, "final_test_mae": 220}
                              for index, (key, variant) in enumerate((("model-a", "RandomForest"), ("model-b", "GRU")), 1)]}
        elif path.endswith("/model-versions/model-b/activate"):
            state["active"] = "model-b"
            body = {"id": "model-b"}
        elif path.endswith("/prediction-window"):
            body = {"production_start_at": "2026-01-01T00:00:00+07:00", "production_end_at": "2027-01-01T00:00:00+07:00"}
        elif path.endswith("/forecast-dashboard"):
            body = {"hourly_24h": [], "daily_7d": []}
        elif path.endswith("/feature-importance"):
            calls.append(route.request.url)
            region_id = path.split("/regions/")[1].split("/")[0]
            active = state["active"] if region_id == "region-a" else None
            body = {"region_id": region_id, "active_model_version_id": active,
                    "model_version": "model_v2" if active == "model-b" else "model_v1",
                    "model_variant": "GRU" if active == "model-b" else "RandomForest",
                    "dataset_filename": "traffic.csv", "status": "ready" if active else "pending",
                    "message": None if active else "This region has no active model.",
                    "generated_at": "2026-10-30T01:00:00Z", "sample_count": 8,
                    "transformed_feature_count": 3, "features": copy.deepcopy(active_rows) if active else [],
                    "history": {"status": "ready", "total_models": 5, "used_reports": 3, "dataset_count": 2,
                        "features": copy.deepcopy(average_rows), "excluded_reports": {"missing": 1, "invalid": 1,
                            "unavailable": 0, "unsupported": 0, "zero": 0},
                        "models": [{"model_version_id": "saved-1", "model_version": "model_v1", "model_variant": "RandomForest",
                                    "dataset_filename": "traffic.csv", "status": "included"},
                                   {"model_version_id": "saved-2", "model_version": "model_v2", "model_variant": "GRU",
                                    "dataset_filename": "traffic.csv", "status": "missing"}]}}
            if active == "model-b":
                body.update(sequence_length=72)
                body["features"][0]["label"] = "Humidity"
            body.pop("history")
            body["model_reports"] = [{"model_name": name, "status": "ready", "message": None,
                "features": copy.deepcopy(active_rows), "sample_count": 8, "generated_at": body["generated_at"],
                "sequence_length": 72 if name in {"LSTM", "GRU"} else None,
                "is_serving": name == body["model_variant"]} for name in ("RandomForest", "XGBoost", "LightGBM", "LSTM", "GRU")]
            body["model_reports"][-1]["features"][0]["label"] = "Humidity"
            body["model_average"] = {"status": "ready", "used_reports": 5, "total_models": 5, "features": copy.deepcopy(average_rows)}
            if not active:
                body["model_reports"] = []
                body["model_average"].update(status="incomplete", used_reports=0, features=[])
            if state["scenario"] == "broken_active":
                body.update(status="unavailable", message="The saved report does not match this model.", features=[])
                body["model_reports"][1].update(status="unavailable", message="No saved report for XGBoost.", features=[])
                body["model_average"].update(status="incomplete", used_reports=4, features=[])
            elif state["scenario"] == "legacy":
                body.pop("model_reports")
                body.pop("model_average")
            elif state["scenario"] == "unavailable":
                status = 503
                body = {"error": {"message": "Reports temporarily unavailable."}}
        elif path.endswith("/drift-checks"):
            names = ["temperature", "humidity", "weather_type", "weather_description", "is_holiday"] + [f"input_{i}" for i in range(8)]
            check = {"id": "check-1", "active_model_version_id": state["active"], "status": "stable", "created_at": "2026-02-01T00:00:00Z",
                "current_start_at": "2026-01-25T00:00:00Z", "current_end_at": "2026-02-01T00:00:00Z",
                "feature_count": 13, "drifted_feature_count": 0, "feature_drift_json": {"summary": {"min_drifted_features": 2},
                    "features": {name: {"type": "numeric", "metric": "psi", "value": .1, "threshold": .2, "drift": False} for name in names}}}
            body = {"items": [check], "latest": check, "active_model_feature_usage": {"model_version_id": state["active"],
                "features": {name: True if name in {"temperature", "weather_type"} else None if name == "is_holiday" else False for name in names}}}
        else:
            body = {"items": []}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "frontend/dist")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cases = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1080})
            context.add_init_script("localStorage.setItem('traffic_token','mock-token')")
            context.route("**/*", respond)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}{BASE}")
            assert not calls
            page.get_by_role("button", name="Feature Importance", exact=True).click()
            history = page.get_by_role("region", name="Average across five models", exact=True)
            active = page.get_by_role("region", name="Model details", exact=True)
            expect(history.locator(".feature-coverage")).to_contain_text("5 of 5")
            expect(history.locator(".feature-row")).to_have_count(10)
            expect(active.locator(".feature-row")).to_have_count(3)
            expect(active.get_by_label("Model", exact=True).locator("option")).to_have_count(5)
            assert history.bounding_box()["y"] < active.bounding_box()["y"]
            active.get_by_label("Model", exact=True).select_option("GRU")
            expect(active.locator(".feature-highlight")).to_contain_text("Humidity")
            active.get_by_label("Model", exact=True).select_option("RandomForest")
            cases.append("two sections, five algorithm choices, selection changes report without Activate")
            history.get_by_role("button", name="Show all 12 inputs").click()
            expect(history.locator(".feature-row")).to_have_count(12)
            expect(active.locator(".feature-row")).to_have_count(3)
            history.get_by_text("Report availability", exact=True).click()
            expect(history).to_contain_text("LightGBM")
            history.get_by_text("View details", exact=True).click()
            expect(history).to_contain_text("20%")
            expect(history).to_contain_text("activated")
            cases.append("show all and inclusion details")
            for width, height in ((1440, 1080), (390, 844), (320, 740)):
                page.set_viewport_size({"width": width, "height": height})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
                assert history.locator(".feature-label").first.evaluate("el => parseFloat(getComputedStyle(el).fontSize)") >= 16
                page.screenshot(path=str(OUTPUT / f"history-{width}.png"), full_page=True)
            cases.append("desktop and mobile 390/320, readable type, contained tables")
            page.set_viewport_size({"width": 1440, "height": 1080})
            page.locator(".region-dropdown-trigger").click()
            page.get_by_role("option", name="No Active Region", exact=False).click()
            expect(history).to_contain_text("Activate a model")
            expect(history.locator(".feature-row")).to_have_count(0)
            expect(active.get_by_label("Model", exact=True)).to_have_count(0)
            assert "model_version_id" not in calls[-1]
            page.locator(".region-dropdown-trigger").click()
            page.get_by_role("option", name="Empty Region", exact=False).click()
            expect(history).to_contain_text("Activate a model")
            cases.append("no borrowing reports without an activated run")
            page.locator(".region-dropdown-trigger").click()
            page.get_by_role("option", name="Demo Region", exact=False).click()
            expect(active).to_contain_text("model_v1")
            page.get_by_role("button", name="Models", exact=True).click()
            page.get_by_role("button", name="Activate", exact=True).last.click()
            page.get_by_role("button", name="Feature Importance", exact=True).click()
            expect(active).to_contain_text("model_v2")
            expect(active.locator(".feature-highlight")).to_contain_text("Humidity")
            active.get_by_text("View details", exact=True).click()
            expect(active).to_contain_text("previous 72 hours")
            cases.append("Activate changes active report including neural context")
            state["scenario"] = "broken_active"
            history.get_by_role("button", name="Refresh", exact=True).click()
            expect(history.locator(".feature-coverage")).to_contain_text("4 of 5")
            expect(history.locator(".feature-row")).to_have_count(0)
            active.get_by_label("Model", exact=True).select_option("XGBoost")
            expect(active).to_contain_text("No saved report for XGBoost")
            active.get_by_label("Model", exact=True).select_option("GRU")
            expect(active.locator(".feature-highlight")).to_contain_text("Humidity")
            state["scenario"] = "legacy"
            history.get_by_role("button", name="Refresh", exact=True).click()
            expect(history).to_contain_text("Update the backend")
            expect(active.locator(".feature-row")).to_have_count(3)
            state["scenario"] = "unavailable"
            history.get_by_role("button", name="Refresh", exact=True).click()
            expect(history.get_by_role("alert")).to_contain_text("temporarily unavailable")
            cases.append("broken active, older backend and request errors")
            state["scenario"] = "ready"
            page.get_by_role("button", name="Drift", exact=True).click()
            page.locator(".drift-layout .selectable-row").first.click()
            feature_scores = page.locator(".drift-layout .card").filter(has=page.get_by_role("heading", name="Feature Scores", exact=True))
            expect(feature_scores.locator("tbody tr")).to_have_count(13)
            expect(feature_scores.locator("tbody tr").filter(has_text="temperature")).to_contain_text("Used")
            expect(feature_scores.locator("tbody tr").filter(has_text="humidity")).to_contain_text("Not used")
            expect(feature_scores.locator("tbody tr").filter(has_text="is_holiday")).to_contain_text("Unknown")
            cases.append("Drift marks active input membership and shows all 13 scores")
            page.get_by_role("button", name="Models", exact=True).click()
            count = len(calls)
            page.wait_for_timeout(300)
            assert len(calls) == count
            state["role"] = "user"
            page.reload()
            expect(page.get_by_role("button", name="Feature Importance", exact=True)).to_have_count(0)
            assert not errors, errors
            assert len(mutations) == 1 and mutations[0].endswith("/model-b/activate"), mutations
            cases.append("inactive tab and non-admin make no feature requests")
            proof = {"passed": True, "cases": cases, "page_errors": errors, "mocked_api_only": True,
                     "live_training_or_writes": False}
            (OUTPUT / "browser-proof.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
            print(json.dumps(proof))
            browser.close()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()

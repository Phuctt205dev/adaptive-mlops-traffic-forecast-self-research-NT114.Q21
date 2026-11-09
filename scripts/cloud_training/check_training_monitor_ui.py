"""Exercise the built monitor with synthetic HTTP fixtures; no production calls."""
import copy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "results/training-monitor/package1-ui"


def fixture():
    names = {"random_forest_lag": "Random Forest", "xgboost_lag": "XGBoost", "lightgbm_lag": "LightGBM", "lstm": "LSTM", "gru": "GRU"}
    models = [{"id": key, "name": name, "state": "waiting", "stage": "queued"} for key, name in names.items()]
    models[0].update(stage="model_cv_completed", cv_completed=True)
    models[1].update(state="running", stage="final_fit_started", fit_purpose="final", cv_completed=True)
    models[2].update(state="evaluated", stage="model_evaluated", ml_completed=True, cv_completed=True)
    for model in models[:3]:
        model.update(source_time="2026-11-09T06:00:00Z", last_ml_received_at="2026-11-09T06:02:00Z", ml_age_seconds=35)
    models[3].update(state="running", stage="epoch_started", fit_purpose="cv_reduced", fold=2, folds=3, epoch=1, epochs=10,
                     source_time="2026-11-09T06:00:00Z", last_ml_received_at="2026-11-09T06:02:00Z", ml_age_seconds=35,
                     fits=[{"fit_id": 1, "fit_purpose": "cv_full", "fold": 1, "seconds": 20, "early_stopped": True, "epochs_completed": 3, "epochs": 10}])
    events = [{"sequence": index, "time": "2026-11-09T06:02:00Z", "level": "info", "code": stage, "model": key,
               "branch": "tree", "details": {"source_time": f"2026-11-09T06:00:0{index}Z"}}
              for index, (key, stage) in enumerate([(key, stage) for key in list(names)[:3]
                                                    for stage in ("model_started", "model_cv_completed")], 1)]
    return {"training_run_id": "synthetic-run", "dataset": "Synthetic monitor fixture.csv", "state": "running", "backend": "kaggle",
            "models": models, "total_models": 5, "evaluated_models": 1, "cv_completed_models": 3, "output_verified_models": 0,
            "events": events, "sequence": 6, "observed_at": "2026-11-09T06:02:35Z", "cancel_requested": False,
            "branches": {branch: {"attempt_id": "synthetic-attempt" if branch != "gru" else None,
                "heartbeat_at": "2026-11-09T06:02:35Z", "observation": {"phase": "ml_running", "telemetry": {
                    "operations": {"status": {"count": 2, "seconds": 3, "bytes": 0}}}}} for branch in ("tree", "lstm", "gru")}}


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    state, errors, writes = {"scenario": "running"}, [], []
    class Handler(SimpleHTTPRequestHandler):
        def translate_path(self, path):
            if "/assets/" in path:
                path = "/assets/" + path.split("/assets/", 1)[1]
            elif not Path(path.split("?", 1)[0]).suffix:
                path = "/index.html"
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
            writes.append(path)
            route.abort()
            return
        status, body = 200, {"items": []}
        if path.endswith("/auth/me"):
            body = {"id": "synthetic-admin", "role": "admin", "email": "synthetic@example.test", "is_active": True}
        elif path.endswith("/regions"):
            body = {"items": [{"id": "synthetic-region", "name": "Synthetic Region", "timezone": "Asia/Ho_Chi_Minh", "is_active": True}]}
        elif path.endswith("/training-monitor/capabilities"):
            body = {"execution_control": True, "cloud_training": True, "default_backend": "kaggle"}
        elif path.endswith("/training-monitor"):
            run = fixture()
            if state["scenario"] in {"failure", "cleaned"}:
                run.update(state="cancelling" if state["scenario"] == "failure" else "failed", cancel_requested=True,
                           error_code="cloud_unavailable")
                run["models"][3].update(failure_origin=True, state="cancelling")
                run["models"][4].update(blocked_by_branch="lstm", state="cancelled", stage="not_started")
                for branch in run["branches"].values():
                    branch.update(stop_confirmed=state["scenario"] == "cleaned", cleanup_confirmed=state["scenario"] == "cleaned")
            body = {"items": [] if state["scenario"] == "completed" else [run]}
            if state["scenario"] == "unavailable":
                status, body = 503, {"error": {"message": "Synthetic unavailable"}}
        elif path.endswith("/forecast-dashboard"):
            body = {"hourly_24h": [], "daily_7d": []}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "frontend/dist")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    checks = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1080})
            context.add_init_script("localStorage.setItem('traffic_token','synthetic-token')")
            context.route("**/*", respond)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.get_by_role("button", name="Training Monitor", exact=True).click()
            card = page.locator(".training-monitor-card")
            expect(card).to_have_count(1)
            model_table = card.locator(".training-monitor-table-wrap")
            expect(model_table.locator("tbody tr")).to_have_count(5)
            expect(model_table.get_by_text("CV finished · Waiting for final training", exact=True)).to_be_visible()
            expect(model_table.get_by_text("Learning · Reduced features · CV 2/3 · Epoch 1/10", exact=True)).to_be_visible()
            expect(model_table.locator("tbody tr").filter(has_text="LSTM")).to_contain_text("Waiting for new ML progress")
            expect(card.locator("progress")).to_have_count(0)
            expect(card.locator(".training-monitor-logs tbody tr")).to_have_count(6)
            checks.append("five-model phases, burst history and stale ML independent of heartbeat")
            page.screenshot(path=str(OUTPUT / "desktop.png"), full_page=True)
            card.get_by_text("Timing and connection details", exact=True).click()
            expect(card.get_by_text("cv full · CV 1 · 20.00s · Early stopping (3/10)", exact=True)).to_be_visible()
            checks.append("separate timestamps, operation timings and actual early-stopped fit")
            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                expect(card).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "Page overflowed mobile viewport"
                page.screenshot(path=str(OUTPUT / f"mobile-{width}.png"), full_page=True)
            checks.append("mobile 390px and 320px, expanded details without page overflow")
            state["scenario"] = "unavailable"
            expect(page.get_by_text("Training updates are unavailable.", exact=False)).to_be_visible(timeout=8000)
            expect(card).to_have_count(1)
            checks.append("unavailable snapshot preserves last known state")
            state["scenario"] = "failure"
            expect(card.get_by_text("Not started · Stopped after LSTM failed", exact=True)).to_be_visible(timeout=8000)
            expect(card.get_by_text("This group failed · Checking stop", exact=True)).to_be_visible()
            expect(card.get_by_role("button", name="Dismiss", exact=True)).to_have_count(0)
            checks.append("failed LSTM versus blocked GRU and unconfirmed stop")
            state["scenario"] = "cleaned"
            expect(card.get_by_role("button", name="Dismiss", exact=True)).to_be_visible(timeout=8000)
            checks.append("dismiss offered only after run reaches failed terminal state")
            state["scenario"] = "completed"
            expect(card).to_have_count(0, timeout=8000)
            expect(page.locator(".training-monitor")).to_have_count(0)
            checks.append("successful run disappears without a retained success card")
            assert not errors, errors
            assert not writes, writes
            browser.close()
        proof = {"checks": checks, "page_errors": errors, "api_writes": writes, "all_api_responses_synthetic": True,
                 "production_accessed": False}
        (OUTPUT / "proof.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
        print(json.dumps(proof))
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()

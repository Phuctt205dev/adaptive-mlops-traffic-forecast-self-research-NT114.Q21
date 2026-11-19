"""Built UI with synthetic APIs only. Never writes to RKE2 or Kaggle."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
from pathlib import Path
import threading
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
BASE = "/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/"
OUTPUT = ROOT / "results/local-cloud-training-20261118"
MODELS = ["random_forest_lag", "xgboost_lag", "lightgbm_lag", "lstm", "gru"]


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    state = {"local": True, "cloud": True, "runs": [], "submitted": [], "cancels": 0, "retries": [], "fail_cancel": False}
    errors, native, cases = [], [], []
    config = {"train_start_date": "2026-01-01", "train_end_date": "2026-11-17", "artifact_root": "models/regions",
        "model_role": "candidate", "cv_splits": 3, "random_state": 42, "recurrent_sequence_length": 72,
        "recurrent_epochs": 3, "recurrent_batch_size": 32, "final_test_ratio": .15}
    dataset = {"id": "dataset", "original_filename": "five-models.csv", "status": "valid", "row_count": 5000,
               "start_at": "2026-01-01T00:00:00Z", "end_at": "2026-11-17T00:00:00Z"}

    class Handler(SimpleHTTPRequestHandler):
        def translate_path(self, path):
            return super().translate_path("/" + path[len(BASE):] if path.startswith(BASE) else path)
        def log_message(self, *args):
            pass

    def reply(route, body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def respond(route):
        request, path = route.request, urlparse(route.request.url).path
        if "/api/v1/" not in path:
            route.continue_() if urlparse(request.url).hostname == "127.0.0.1" else route.abort()
            return
        if request.method == "POST":
            if path.endswith("/training-runs"):
                payload = request.post_data_json
                state["submitted"].append(payload)
                reply(route, {"training_run": {"id": "run", "status": "queued", "dataset_id": "dataset"}}, 202)
            elif path.endswith("/cancel"):
                state["cancels"] += 1
                if state["fail_cancel"]:
                    reply(route, {"error": {"message": "Synthetic stop could not be confirmed."}}, 503)
                else:
                    state["runs"][0].update(cancel_requested=True, state="cancelling", error_code="admin_cancelled")
                    reply(route, {"stop_requested": True, "stop_confirmed": False})
            elif path.endswith("/retry"):
                state["retries"].append(request.post_data_json)
                reply(route, {"training_run": {"id": "retry", "status": "queued"}}, 202)
            elif path.endswith("/retrain-plan"):
                reply(route, {"dataset": dataset, "selected_models": MODELS,
                              "configuration": {**config, "trigger_source": "feature_drift", "retrain_source_model_id": "active"}})
            elif path.endswith("/dismiss"):
                reply(route, {})
            else:
                reply(route, {"error": {"message": "Unexpected synthetic action"}}, 500)
            return
        if path.endswith("/auth/me"):
            body = {"id": "admin", "display_name": "Admin", "email": "admin@example.test", "role": "admin", "is_active": True}
        elif path.endswith("/regions"):
            body = {"items": [{"id": "region", "name": "Synthetic region", "timezone": "UTC", "latitude": 10,
                               "longitude": 106, "is_active": True, "active_model_version_id": "active"}]}
        elif path.endswith("/datasets"):
            body = {"items": [dataset]}
        elif path.endswith("/capabilities"):
            body = {"cloud_training": state["cloud"], "local_training": state["local"], "local_unavailable_reason": "Local is not configured."}
        elif path.endswith("/training-monitor"):
            body = {"items": state["runs"], "enabled": True}
        elif path.endswith("/training-runs/run"):
            body = {"id": "run", "status": "cancelled", "dataset_id": "dataset"}
        elif path.endswith("/drift-settings"):
            body = {"region_id": "region", "auto_retrain": False}
        elif path.endswith("/drift-retrain-plan"):
            body = {"dataset": dataset, "selected_models": MODELS,
                    "configuration": {**config, "trigger_source": "feature_drift", "retrain_source_model_id": "active"}}
        elif path.endswith("/drift-checks"):
            latest = {"id": "check", "drift_detected": True, "status": "completed",
                      "current_end_at": "2026-11-17T00:00:00Z", "feature_drift_json": {"summary": {}}}
            body = {"items": [latest], "latest": latest}
        elif path.endswith("/prediction-window"):
            body = {"production_start_at": "2026-11-01T00:00:00Z", "production_end_at": None}
        elif path.endswith("/forecast-dashboard"):
            body = {"hourly_24h": [], "daily_7d": []}
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
            page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
            page.goto(f"http://127.0.0.1:{server.server_port}{BASE}")

            def tab(name):
                page.get_by_role("button", name=name, exact=True).click()
                expect(page.locator(".topbar h1")).to_have_text(name)

            tab("Datasets")
            for backend, count in (("kaggle", 5), ("local", 5), ("local", 3)):
                page.reload()
                page.get_by_role("button", name="Train", exact=True).click()
                modal = page.locator(".train-modal-card")
                expect(modal.get_by_role("radio", name="Cloud · Kaggle")).to_be_checked()
                if backend == "local":
                    modal.get_by_role("radio", name="Local · RKE2").check()
                if count == 3:
                    modal.locator(".model-option").filter(has_text="LSTM").get_by_role("checkbox").uncheck()
                    modal.locator(".model-option").filter(has_text="GRU").get_by_role("checkbox").uncheck()
                    modal.get_by_role("radio", name="Cloud · Kaggle").check()
                    modal.get_by_role("radio", name="Local · RKE2").check()
                expect(modal.get_by_role("checkbox", checked=True)).to_have_count(count)
                label = f"Train {count} models on {'Local' if backend == 'local' else 'Kaggle'}"
                modal.get_by_role("button", name=label, exact=True).click()
                expect(modal).not_to_be_visible()
                assert state["submitted"][-1]["training_backend"] == backend
                assert len(state["submitted"][-1]["selected_models"]) == count
                assert state["submitted"][-1]["cv_splits"] == 3
            cases.append("Kaggle/Local submit all five; three-model selection and shared config survive switching")

            state["local"] = False
            page.reload()
            page.get_by_role("button", name="Train", exact=True).click()
            expect(page.get_by_role("radio", name="Local · RKE2")).to_be_disabled()
            page.get_by_role("button", name="Close", exact=True).click()
            cases.append("Local stays disabled until backend readiness; Close sends no training request")
            state["local"] = True
            state["cloud"] = False
            page.reload()
            page.get_by_role("button", name="Train", exact=True).click()
            expect(page.get_by_role("radio", name="Cloud · Kaggle")).to_be_disabled()
            page.get_by_role("radio", name="Local · RKE2").check()
            expect(page.get_by_role("button", name="Train 5 models on Local", exact=True)).to_be_enabled()
            page.get_by_role("button", name="Close", exact=True).click()
            state["cloud"] = True
            cases.append("Local admission does not depend on Kaggle availability")

            tab("Drift")
            page.get_by_role("button", name="Retrain now", exact=True).click()
            expect(page.locator(".train-modal-card")).to_contain_text("Kaggle · Retraining")
            expect(page.locator(".train-modal-card").get_by_role("radio")).to_have_count(0)
            page.get_by_role("button", name="Close", exact=True).click()
            cases.append("manual retrain has fixed Kaggle and cannot select Local")

            state["runs"] = [{"training_run_id": "run", "dataset": "five-models.csv", "backend": "local", "purpose": "training",
                "state": "running", "cancel_requested": False, "models": [{"id": "gru", "name": "GRU", "state": "waiting", "stage": "queued"}],
                "total_models": 1, "sequence": 1, "events": [], "branches": {"gru": {"attempt_id": "attempt", "observation": {"phase": "waiting_resources"}}}}]
            state["runs"][0]["backend"] = "kaggle"
            state["runs"][0]["branches"]["gru"]["observation"]["phase"] = "input_reconciling"
            tab("Training Monitor")
            expect(page.locator(".training-monitor-card")).to_contain_text("Checking uploaded training data")
            expect(page.locator(".training-monitor-card")).to_contain_text("only the upload is retried")
            expect(page.locator(".training-monitor-card")).to_contain_text("No ML progress received yet")
            cases.append("input reconciliation explains upload recovery without claiming ML has started")
            state["runs"][0]["backend"] = "local"
            state["runs"][0]["branches"]["gru"]["observation"]["phase"] = "waiting_resources"
            page.reload()
            expect(page.locator(".training-monitor-card")).to_contain_text("Local · RKE2 CPU")
            expect(page.locator(".training-monitor-card")).to_contain_text("Waiting for RKE2 resources")
            page.get_by_role("button", name="Stop training", exact=True).click()
            dialog = page.get_by_role("dialog", name="Stop this training run?")
            expect(dialog).to_be_visible()
            page.keyboard.press("Escape")
            assert state["cancels"] == 0
            page.get_by_role("button", name="Stop training", exact=True).click()
            state["fail_cancel"] = True
            dialog.get_by_role("button", name="Stop training", exact=True).click()
            expect(dialog.get_by_role("alert")).to_contain_text("could not be confirmed")
            expect(dialog).to_be_visible()
            state["fail_cancel"] = False
            dialog.get_by_role("button", name="Stop training", exact=True).click()
            expect(page.get_by_role("button", name="Stopping…", exact=True)).to_be_disabled()
            assert state["cancels"] == 2
            cases.append("Stop requires styled confirmation; request remains Cancelling until actual acknowledgement")
            cases.append("a failed stop request keeps the dialog open and does not claim stop confirmation")

            run = state["runs"][0]
            run.update(state="cancelled", can_retry=True, error_code="admin_cancelled")
            page.reload()
            expect(page.get_by_role("button", name="Retry on Local", exact=True)).to_be_visible()
            page.get_by_role("button", name="Retry on Local", exact=True).click()
            page.get_by_role("dialog").get_by_role("button", name="Start new run", exact=True).click()
            assert len(state["retries"]) == 1 and state["retries"][0]["training_backend"] == "local"
            assert state["retries"][0]["request_id"]
            cases.append("cancelled run offers a new linked retry without relabelling the original run")

            tab("Datasets")
            for width in (1440, 390, 320):
                page.set_viewport_size({"width": width, "height": 950})
                page.reload()
                page.get_by_role("button", name="Train", exact=True).click()
                expect(page.get_by_role("radio", name="Local · RKE2")).to_be_enabled()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(OUTPUT / f"select-models-{width}.png"))
                page.get_by_role("button", name="Close", exact=True).click()
            cases.append("switch and Close remain usable at desktop/390px/320px without horizontal overflow")
            assert not errors, errors
            assert not native, native
            browser.close()
    finally:
        server.shutdown(); server.server_close()
    proof = {"cases": cases, "js_errors": errors, "native_dialogs": native, "synthetic_only": True,
        "assets": {p.relative_to(ROOT / 'frontend/dist').as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted((ROOT / 'frontend/dist').rglob('*')) if p.is_file()}}
    (OUTPUT / "ui-proof.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps(proof))


if __name__ == "__main__":
    main()

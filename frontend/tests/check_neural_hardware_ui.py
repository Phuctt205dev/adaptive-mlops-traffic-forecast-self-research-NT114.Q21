"""Built UI with synthetic APIs only: LSTM/GRU hardware choice and the long-queue notice.

Never writes to RKE2 or Kaggle. Run `npm run build` in frontend/ first.
"""
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
BASE = "/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/"
MODELS = ["random_forest_lag", "xgboost_lag", "lightgbm_lag", "lstm", "gru"]
ATTEMPT = "0a1b2c3d-1111-2222-3333-444455556666"


def monitor_run(waited_minutes, state="running"):
    now = datetime.now(UTC)
    return {"training_run_id": "run", "dataset": "five-models.csv", "backend": "kaggle", "purpose": "training",
            "state": state, "cancel_requested": state == "cancelling", "observed_at": now.isoformat(),
            "models": [{"id": "lstm", "name": "LSTM", "state": "waiting", "stage": "queued"}], "total_models": 1,
            "sequence": 1, "events": [],
            "branches": {"lstm": {"attempt_id": ATTEMPT, "observation": {
                "provider_state": "QUEUED", "phase": "provider_wait",
                "provider_observed_at": (now - timedelta(minutes=waited_minutes)).isoformat()}}}}


def main():
    state = {"submitted": [], "runs": []}
    errors, cases = [], []
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
                state["submitted"].append(request.post_data_json)
                reply(route, {"training_run": {"id": "run", "status": "queued", "dataset_id": "dataset"}}, 202)
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
            body = {"cloud_training": True, "local_training": True, "local_unavailable_reason": ""}
        elif path.endswith("/training-monitor"):
            body = {"items": state["runs"], "enabled": True}
        elif path.endswith("/drift-settings"):
            body = {"region_id": "region", "auto_retrain": False}
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

            def tab(name):
                page.get_by_role("button", name=name, exact=True).click()
                expect(page.locator(".topbar h1")).to_have_text(name)

            def train_from_modal(summary):
                page.get_by_role("button", name="Train", exact=True).click()
                modal = page.locator(".train-modal-card")
                expect(modal).to_contain_text(summary)
                modal.get_by_role("button", name="Train 5 models on Kaggle", exact=True).click()
                expect(modal).not_to_be_visible()

            tab("Datasets")
            page.get_by_role("button", name="Advanced", exact=True).click()
            select = page.get_by_label("Kaggle hardware")
            expect(select).to_have_value("gpu")
            expect(page.locator(".training-config-advanced")).to_contain_text("Local training always uses the CPU")
            train_from_modal("Kaggle GPU")
            assert state["submitted"][-1]["neural_accelerator"] == "gpu"
            cases.append("untouched form still asks for GPU")

            page.reload()
            page.get_by_role("button", name="Advanced", exact=True).click()
            select.select_option("cpu")
            train_from_modal("Kaggle CPU")
            assert state["submitted"][-1]["neural_accelerator"] == "cpu"
            assert state["submitted"][-1]["training_backend"] == "kaggle"
            assert len(state["submitted"][-1]["selected_models"]) == 5
            cases.append("choosing CPU is shown before starting and reaches the request")

            page.get_by_role("button", name="Expand training configuration").click()
            dialog = page.get_by_role("dialog")
            expect(dialog.get_by_label("Kaggle hardware")).to_have_value("cpu")
            dialog.get_by_role("button", name="Done", exact=True).click()
            cases.append("the choice survives the expanded view")

            for width in (1440, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                assert overflow <= 1, f"horizontal scroll at {width}px: {overflow}"
            cases.append("no horizontal scroll at 1440, 390 and 320 px")
            page.set_viewport_size({"width": 1440, "height": 1050})

            page.reload()
            for width in (1440, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                page.get_by_role("button", name="Train", exact=True).click()
                card = page.locator(".train-modal-card")
                expect(card.locator(".fairness-panel")).to_contain_text("LSTM/GRU hardware")
                spill = card.evaluate("el => el.scrollWidth - el.clientWidth")
                # At 320px this card already spills 24px without the new tile; the tile must add nothing.
                card.evaluate("el => el.querySelector('.fairness-panel div:last-child').remove()")
                assert spill <= card.evaluate("el => el.scrollWidth - el.clientWidth") + 1,                     f"the hardware tile widens Select models at {width}px: {spill}"
                page.get_by_role("button", name="Close", exact=True).click()
            cases.append("Select models summary with the hardware tile adds no overflow at 1440, 390 and 320 px")
            page.set_viewport_size({"width": 1440, "height": 1050})

            for waited, shown in ((5, False), (20, True)):
                state["runs"] = [monitor_run(waited)]
                page.reload()
                tab("Training Monitor")
                card = page.locator(".training-monitor-card")
                if shown:
                    expect(card).to_contain_text(f"LSTM has waited {waited} minutes in Kaggle's queue")
                    expect(card).to_contain_text("traffic-train-0a1b2c3d111122223333444455556666")
                    expect(card).to_contain_text("choose CPU for LSTM and GRU")
                else:
                    expect(card).to_contain_text("Queued on Kaggle")
                    expect(card).not_to_contain_text("has waited")
            cases.append("a queue under 15 minutes is only labelled; past 15 minutes the notebook and options are shown")

            state["runs"] = [monitor_run(40, state="cancelling")]
            state["runs"][0]["branches"]["lstm"]["observation"]["stop_waiting_since"] = datetime.now(UTC).isoformat()
            page.reload()
            tab("Training Monitor")
            card = page.locator(".training-monitor-card")
            expect(card).to_contain_text("Stop was sent, but Kaggle still has this notebook")
            expect(card).not_to_contain_text("has waited")
            cases.append("during Stop only the stop explanation is shown")
            browser.close()
    finally:
        server.shutdown()
    assert not errors, errors
    print(json.dumps({"passed": cases}, indent=2))


if __name__ == "__main__":
    main()

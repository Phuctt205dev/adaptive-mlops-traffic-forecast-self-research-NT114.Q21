"""Exercise built admin UX against intercepted synthetic APIs; no live writes."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
BASE = "/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/"
OUTPUT = ROOT / "results/admin-dialogs-20261116"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    state = {"role": "admin", "user": "admin-a", "fail": False, "hold": False,
        "regions": ["region-a", "region-b"], "models": ["candidate", "active"]}
    deletes, pending, native, errors, unexpected, cases = [], [], [], [], [], []

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
        if request.method == "DELETE" and ("/model-versions/" in path or "/regions/" in path):
            deletes.append(path)
            if state["fail"]:
                reply(route, {"error": {"message": "Synthetic item is busy. Please retry."}}, 409)
                return
            if state["hold"]:
                pending.append(route)
                return
            if "/model-versions/" in path:
                state["models"].remove(path.rsplit("/", 1)[1])
            else:
                state["regions"].remove(path.rsplit("/", 1)[1])
            reply(route, {})
            return
        if request.method not in {"GET", "OPTIONS"}:
            unexpected.append({"method": request.method, "path": path})
            reply(route, {"error": {"message": "Unexpected synthetic mutation"}}, 500)
            return
        if path.endswith("/cleanup-status"):
            body = {"state": "pending" if state["hold"] else "not_started", "phase": "preparing",
                    "done": [], "can_retry": not state["hold"]}
        elif path.endswith("/auth/me"):
            body = {"id": state["user"], "display_name": "Admin", "email": "admin@example.test", "role": state["role"], "is_active": True}
        elif path.endswith("/regions"):
            body = {"items": [{"id": rid, "name": {"region-a": "Alpha", "region-b": "Beta"}[rid],
                "latitude": 10, "longitude": 106, "timezone": "UTC", "is_active": True,
                "active_model_version_id": "active"} for rid in state["regions"]]}
        elif path.endswith("/model-versions"):
            body = {"items": [{"id": mid, "version": "model_v10" if mid == "candidate" else "model_v1",
                "variant": "RandomForest", "status": mid, "cv_mean_mae": 399, "final_test_mae": 313,
                "created_at": "2026-11-16T10:00:00Z"} for mid in state["models"]]}
        elif path.endswith("/datasets"):
            body = {"items": [{"id": "dataset", "original_filename": "demo.csv", "status": "valid", "row_count": 1000,
                "data_start_at": "2026-01-01T00:00:00Z", "data_end_at": "2026-11-01T00:00:00Z"}]}
        elif path.endswith("/prediction-window"):
            body = {"production_start_at": "2026-11-01T00:00:00Z", "production_end_at": "2026-11-16T00:00:00Z"}
        elif path.endswith("/forecast-dashboard"):
            body = {"hourly_24h": [], "daily_7d": []}
        elif path.endswith("/drift-settings"):
            body = {"region_id": path.split("/regions/")[1].split("/")[0], "auto_retrain": False}
        elif path.endswith("/drift-checks"):
            body = {"items": [], "latest": None}
        elif path.endswith("/capabilities"):
            body = {"cloud_training": True}
        else:
            body = {"items": []}
        reply(route, body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "frontend/dist")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1050})
            context.add_init_script("if (location.hostname === '127.0.0.1') localStorage.setItem('traffic_token','synthetic-token')")
            context.route("**/*", respond)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: (native.append(dialog.message), dialog.dismiss()))
            page.goto(f"http://127.0.0.1:{server.server_port}{BASE}")

            def tab(name):
                page.get_by_role("button", name=name, exact=True).click()
                expect(page.locator(".topbar h1")).to_have_text(name)

            for name in ("Models", "Datasets", "Drift"):
                tab(name)
                page.reload()
                expect(page.locator(".topbar h1")).to_have_text(name)
            page.locator(".region-dropdown-trigger").click()
            page.get_by_role("option", name="Beta", exact=False).click()
            page.reload()
            expect(page.locator(".topbar h1")).to_have_text("Drift")
            expect(page.locator(".region-dropdown-trigger")).to_contain_text("Beta")
            page.locator(".profile-card").click()
            page.reload()
            expect(page.locator(".topbar h1")).to_have_text("Profile")
            cases.append("reload keeps Models/Datasets/Drift/Profile and existing region selection")

            tab("Models")
            delete_button = page.get_by_role("button", name="Delete", exact=True).first
            delete_button.click()
            dialog = page.get_by_role("alertdialog", name="Delete model?")
            expect(dialog).to_be_visible()
            expect(dialog).to_contain_text("model_v10")
            expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_be_focused()
            assert page.evaluate("document.body.style.overflow") == "hidden"
            for _ in range(8):
                page.keyboard.press("Tab")
                assert dialog.evaluate("el => el.contains(document.activeElement)")
            page.keyboard.press("Escape")
            expect(dialog).not_to_be_visible()
            expect(delete_button).to_be_focused()
            assert not deletes and not native
            assert page.evaluate("document.body.style.overflow") != "hidden"
            delete_button.click()
            dialog.get_by_role("button", name="Cancel", exact=True).press("Enter")
            expect(dialog).not_to_be_visible()
            assert not deletes
            cases.append("styled confirmation has no native popup, traps focus, Cancel/Enter and Escape are safe")

            delete_button.click()
            page.mouse.click(5, 5)
            expect(dialog).not_to_be_visible()
            state["fail"] = True
            delete_button.click()
            dialog.get_by_role("button", name="Delete model", exact=True).click()
            expect(dialog.get_by_role("alert")).to_contain_text("Synthetic item is busy")
            expect(dialog.get_by_role("button", name="Retry deletion", exact=True)).to_be_enabled()
            page.screenshot(path=str(OUTPUT / "delete-model-error.png"))
            state.update(fail=False, hold=True)
            dialog.get_by_role("button", name="Retry deletion", exact=True).click()
            expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_disabled()
            expect(dialog.get_by_role("button", name="Close dialog")).to_be_disabled()
            expect(dialog.get_by_role("button", name="Deleting…", exact=True)).to_be_disabled()
            page.keyboard.press("Escape")
            page.mouse.click(5, 5)
            expect(dialog).to_be_visible()
            assert len(deletes) == 2 and len(pending) == 1
            state["models"].remove("candidate")
            reply(pending.pop(), {})
            state["hold"] = False
            expect(dialog).not_to_be_visible()
            expect(page.get_by_role("cell", name="model_v10", exact=True)).to_have_count(0)
            cases.append("delete errors remain in modal; in-flight delete blocks duplicate submit and dismissal")

            tab("Regions")
            region_button = page.get_by_role("row").filter(has_text="Alpha").get_by_role("button", name="Delete region")
            region_button.click()
            region_dialog = page.get_by_role("alertdialog", name="Delete region?")
            expect(region_dialog).to_contain_text("Alpha")
            expect(region_dialog).to_contain_text("datasets, models")
            region_dialog.get_by_role("button", name="Cancel", exact=True).click()
            assert len(deletes) == 2
            cases.append("region confirmation names the selected region and explains the deletion scope")

            tab("Datasets")
            config = page.get_by_role("region", name="Training Configuration", exact=True)
            upload = page.locator(".card").filter(has=page.get_by_role("heading", name="Upload Dataset CSV", exact=True))
            expect(config.get_by_label("Start date", exact=True)).to_have_attribute("type", "date")
            config.get_by_role("button", name="Advanced", exact=True).click()
            expect(config.get_by_label("CV folds", exact=True)).to_be_visible()
            assert abs(config.bounding_box()["height"] - upload.bounding_box()["height"]) <= 2
            config.get_by_role("button", name="Expand training configuration").click()
            expanded = page.get_by_role("dialog", name="Advanced training configuration", exact=True)
            expect(expanded).to_be_visible()
            assert expanded.bounding_box()["width"] >= 900
            expanded.get_by_label("CV folds", exact=True).fill("4")
            expanded.get_by_label("Random seed", exact=True).fill("17")
            expanded.get_by_label("Epoch limit per CV fold", exact=True).fill("9")
            expanded.get_by_label("Start date", exact=True).fill("2026-02-01")
            expanded.get_by_label("End date (excluded)", exact=True).fill("2026-10-01")
            expect(expanded.get_by_label("artifact_root", exact=True)).to_have_attribute("readonly", "")
            page.screenshot(path=str(OUTPUT / "configuration-expanded-desktop.png"))
            page.mouse.click(5, 5)
            expect(expanded).not_to_be_visible()
            expect(config.get_by_label("CV folds", exact=True)).to_have_value("4")
            expect(config.get_by_label("Random seed", exact=True)).to_have_value("17")
            expect(config.get_by_label("Start date", exact=True)).to_have_value("2026-02-01")
            cases.append("expanded settings share values with equal-height card; outside dismissal keeps all edits")

            for dismiss in ("Escape", "Done", "Close dialog"):
                config.get_by_role("button", name="Expand training configuration").click()
                expect(expanded.get_by_label("Epoch limit per CV fold", exact=True)).to_have_value("9")
                if dismiss == "Escape":
                    page.keyboard.press("Escape")
                else:
                    expanded.get_by_role("button", name=dismiss, exact=True).click()
                expect(expanded).not_to_be_visible()
                expect(config.get_by_role("button", name="Expand training configuration")).to_be_focused()
            config.get_by_role("button", name="Basic", exact=True).click()
            config.get_by_role("button", name="Advanced", exact=True).click()
            expect(config.get_by_label("CV folds", exact=True)).to_have_value("4")
            page.get_by_role("button", name="Train", exact=True).click()
            expect(page.locator(".train-modal-card .fairness-panel")).to_contain_text("4")
            expect(page.locator(".train-modal-dataset")).to_contain_text("2026-02-01 - 2026-10-01")
            page.locator(".train-modal-card").get_by_role("button", name="Close", exact=True).click()
            cases.append("Escape/Done/X and Basic/Advanced retain edits; train selection receives the edited configuration")

            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 844})
                config.get_by_role("button", name="Expand training configuration").click()
                expect(expanded).to_be_visible()
                box = expanded.bounding_box()
                assert box["width"] <= width and box["height"] <= 844 and box["x"] >= 0
                assert expanded.evaluate("el => el.scrollWidth <= el.clientWidth")
                assert expanded.locator(".app-dialog-body").evaluate("el => el.scrollHeight > el.clientHeight")
                page.screenshot(path=str(OUTPUT / f"configuration-expanded-{width}.png"))
                page.keyboard.press("Escape")
            cases.append("expanded configuration fits mobile 390/320 with internal scrolling")

            page.set_viewport_size({"width": 1440, "height": 1050})
            tab("Models")
            page.get_by_role("button", name="Delete", exact=True).first.click()
            expect(dialog).to_be_visible()
            page.screenshot(path=str(OUTPUT / "delete-model-desktop.png"))
            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 844})
                box = dialog.bounding_box()
                assert box["width"] <= width and box["height"] <= 844
                assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
                page.screenshot(path=str(OUTPUT / f"delete-model-{width}.png"))
            page.keyboard.press("Escape")
            cases.append("confirmation stays readable on desktop and mobile")

            state["role"] = "user"
            page.reload()
            expect(page.locator(".topbar h1")).to_have_text("Dashboard")
            expect(page.get_by_role("button", name="Models", exact=True)).to_have_count(0)
            state.update(role="admin", user="admin-b")
            page.reload()
            expect(page.locator(".topbar h1")).to_have_text("Dashboard")
            state["user"] = "admin-a"
            page.reload()
            expect(page.locator(".topbar h1")).to_have_text("Models")
            cases.append("account switching isolates saved tabs and demoted users cannot restore an admin tab")
            assert not errors and not native and not unexpected, (errors, native, unexpected)
            (OUTPUT / "browser-proof.json").write_text(json.dumps({"cases": cases, "page_errors": errors,
                "native_dialogs": native, "synthetic_deletes": deletes, "unexpected_mutations": unexpected,
                "synthetic_api_only": True}, indent=2), encoding="utf-8")
            print(json.dumps({"passed": len(cases), "cases": cases, "page_errors": errors, "native_dialogs": native}))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

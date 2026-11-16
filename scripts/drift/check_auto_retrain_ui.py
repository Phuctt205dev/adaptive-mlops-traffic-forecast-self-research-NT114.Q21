"""Built UI regression with synthetic API only; never contact or train on RKE2."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
BASE = "/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/"
OUTPUT = ROOT / "results/drift-settings-20261116"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    state = {"enabled": {"region-a": False, "region-b": False}, "hold_get": None,
        "hold_patch": None, "fail_patch": False, "fail_get": False, "lost_response": False, "wrong_region": False}
    pending_get, pending_patch, writes, checks, errors, cases = [], [], [], [], [], []

    class Handler(SimpleHTTPRequestHandler):
        def translate_path(self, path):
            if path.startswith(BASE):
                path = "/" + path[len(BASE):]
            return super().translate_path(path)

        def log_message(self, *args):
            pass

    def fulfill(route, body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def respond(route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if "/api/v1/" not in path:
            route.continue_() if parsed.hostname == "127.0.0.1" else route.abort()
            return
        if path.endswith("/auth/me"):
            body = {"id": "admin", "email": "admin@example.test", "role": "admin", "is_active": True}
        elif path.endswith("/regions"):
            body = {"items": [{"id": rid, "name": name, "timezone": "UTC", "is_active": True,
                "active_model_version_id": None} for rid, name in (("region-a", "Alpha"), ("region-b", "Beta"))]}
        elif path.endswith("/drift-settings"):
            rid = path.split("/regions/")[1].split("/")[0]
            if request.method == "PATCH":
                enabled = request.post_data_json["auto_retrain"]
                writes.append({"region_id": rid, "auto_retrain": enabled})
                if state["fail_patch"]:
                    fulfill(route, {"error": {"message": "Synthetic save rejected"}}, 503)
                    return
                if state["hold_patch"] == rid:
                    pending_patch.append((route, rid, enabled))
                    return
                state["enabled"][rid] = enabled
                if state["lost_response"]:
                    route.abort()
                    return
            elif state["fail_get"]:
                fulfill(route, {"error": {"message": "Synthetic settings unavailable"}}, 503)
                return
            body = {"region_id": "wrong-region" if state["wrong_region"] else rid,
                "auto_retrain": state["enabled"][rid]}
            if request.method == "GET" and state["hold_get"] == rid:
                pending_get.append((route, body))
                return
        elif path.endswith("/drift-checks/run"):
            checks.append({"region_id": path.split("/regions/")[1].split("/")[0], "params": parse_qs(parsed.query)})
            body = {"checks": []}
        elif path.endswith("/drift-checks"):
            body = {"items": [], "latest": None, "active_model_feature_usage": None}
        else:
            body = {"items": []}
        fulfill(route, body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "frontend/dist")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1050})
            context.add_init_script("localStorage.setItem('traffic_token', 'synthetic-token')")
            context.route("**/*", respond)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}{BASE}")

            def drift_tab():
                page.get_by_role("button", name="Drift", exact=True).click()

            def switch(name):
                page.locator(".region-dropdown-trigger").click()
                page.get_by_role("option", name=name, exact=False).click()

            drift_tab()
            toggle = page.get_by_role("checkbox", name="Auto retrain", exact=True)
            check_now = page.get_by_role("button", name="Check now", exact=True)
            expect(toggle).to_be_enabled()
            expect(toggle).not_to_be_checked()
            assert not writes
            toggle.check()
            expect(toggle).to_be_enabled()
            expect(toggle).to_be_checked()
            assert state["enabled"] == {"region-a": True, "region-b": False}
            assert len(writes) == 1 and not checks
            cases.append("opt-in saves immediately for A without Check now or training")

            switch("Beta")
            expect(toggle).to_be_enabled()
            expect(toggle).not_to_be_checked()
            switch("Alpha")
            expect(toggle).to_be_enabled()
            expect(toggle).to_be_checked()
            page.reload()
            drift_tab()
            expect(toggle).to_be_enabled()
            expect(toggle).to_be_checked()
            page.get_by_role("button", name="Dashboard", exact=True).click()
            drift_tab()
            expect(toggle).to_be_checked()
            cases.append("region switching, reload and tab switching preserve A while B stays off")

            check_now.click()
            expect(check_now).to_be_enabled()
            assert checks[-1]["region_id"] == "region-a" and checks[-1]["params"]["auto_retrain"] == ["true"]
            toggle.uncheck()
            expect(toggle).to_be_enabled()
            check_now.click()
            expect(check_now).to_be_enabled()
            assert checks[-1]["params"]["auto_retrain"] == ["false"]
            switch("Beta")
            expect(toggle).to_be_enabled()
            toggle.check()
            expect(toggle).to_be_enabled()
            switch("Alpha")
            expect(toggle).to_be_enabled()
            expect(toggle).not_to_be_checked()
            cases.append("opt-out is saved, B can opt in independently, checks send the selected region state")

            state["hold_patch"] = "region-a"
            toggle.check()
            expect(toggle).to_be_disabled()
            expect(check_now).to_be_disabled()
            switch("Beta")
            expect(toggle).to_be_enabled()
            expect(toggle).to_be_checked()
            assert pending_patch
            route, rid, enabled = pending_patch.pop()
            state["enabled"][rid] = enabled
            fulfill(route, {"region_id": rid, "auto_retrain": enabled})
            state["hold_patch"] = None
            expect(toggle).to_be_checked()
            toggle.uncheck()
            expect(toggle).to_be_enabled()
            switch("Alpha")
            expect(toggle).to_be_checked()
            expect(toggle).to_be_enabled()
            cases.append("saving blocks duplicate action; late A save never changes B")

            state["hold_get"] = "region-a"
            switch("Beta")
            expect(toggle).to_be_enabled()
            switch("Alpha")
            expect(toggle).to_be_disabled()
            switch("Beta")
            expect(toggle).to_be_enabled()
            expect(toggle).not_to_be_checked()
            assert pending_get
            for route, body in pending_get:
                fulfill(route, body)
            pending_get.clear()
            state["hold_get"] = None
            expect(toggle).not_to_be_checked()
            cases.append("late A load never restores A's tick on B")

            state["fail_patch"] = True
            toggle.check()
            expect(toggle).to_be_enabled()
            expect(toggle).not_to_be_checked()
            expect(page.get_by_text("Could not save or confirm", exact=False)).to_be_visible()
            assert state["enabled"]["region-b"] is False
            state["fail_patch"] = False
            cases.append("rejected save re-reads the server and restores confirmed value")

            state["lost_response"] = True
            toggle.check()
            expect(toggle).to_be_enabled()
            expect(toggle).to_be_checked()
            assert state["enabled"]["region-b"] is True
            state["lost_response"] = False
            cases.append("lost PATCH response reconciles committed value instead of falsely rolling back")

            state.update(fail_patch=True, fail_get=True)
            toggle.uncheck()
            expect(page.get_by_role("button", name="Retry setting")).to_be_visible()
            expect(toggle).to_be_disabled()
            expect(check_now).to_be_disabled()
            state.update(fail_patch=False, fail_get=False)
            page.get_by_role("button", name="Retry setting").click()
            expect(toggle).to_be_enabled()
            expect(toggle).to_be_checked()
            cases.append("unconfirmed state disables controls and supports retry")

            state["wrong_region"] = True
            switch("Alpha")
            expect(page.get_by_role("button", name="Retry setting")).to_be_visible()
            expect(toggle).to_be_disabled()
            state["wrong_region"] = False
            page.get_by_role("button", name="Retry setting").click()
            expect(toggle).to_be_enabled()
            expect(toggle).to_be_checked()
            cases.append("response with mismatched region is rejected")

            for width in (1440, 390, 320):
                page.set_viewport_size({"width": width, "height": 1000})
                page.screenshot(path=str(OUTPUT / f"drift-{width}.png"), full_page=True)
                overflow = page.evaluate("""() => [...document.querySelectorAll('body *')]
                    .filter(el => el.getBoundingClientRect().width && el.getBoundingClientRect().right > innerWidth + 1)
                    .slice(0, 12).map(el => ({tag: el.tagName, cls: el.className, width: el.getBoundingClientRect().width}))""")
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (width, overflow)
            cases.append("desktop and mobile show controls without horizontal overflow")
            assert not errors, errors
            (OUTPUT / "browser-proof.json").write_text(json.dumps({"cases": cases, "writes": writes,
                "checks": checks, "page_errors": errors, "synthetic_api_only": True}, indent=2), encoding="utf-8")
            print(json.dumps({"passed": len(cases), "cases": cases, "page_errors": errors}))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

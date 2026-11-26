"""Built UI with synthetic APIs only: Model Quality error share, last-week comparison and low-traffic tag.

Never calls RKE2, Kaggle or GitHub. Run `npm run build` in frontend/ first.
Usage: python frontend/tests/check_model_quality_ui.py [SCREENSHOT_DIR]
"""
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import random
import sys
import threading
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
BASE = "/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/"
ZONE = timezone(timedelta(hours=7))
REGION, MODEL = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
LOW_TRAFFIC_SHARE = 0.25


def make_report(hours, *, baseline_ratio, old_backend=False):
    """Synthetic daily traffic. The last hour is 03:00 (low traffic) and one hour has 0 vehicles."""
    rng = random.Random(7)
    end = datetime(2026, 11, 26, 4, tzinfo=ZONE)
    start = end - timedelta(hours=hours)
    points = []
    for index in range(hours):
        when = start + timedelta(hours=index)
        actual = round(300 + 4200 * max(0.0, math.sin(math.pi * (when.hour - 5) / 16)))
        if index == 22:
            actual = 0
        predicted = max(0.0, actual * (1 + rng.uniform(-0.12, 0.12)) + rng.uniform(-60, 60))
        error = abs(actual - predicted)
        points.append({"forecast_for": when.isoformat(), "reference": actual, "prediction": round(predicted, 1),
                       "absolute_error": round(error, 1), "label_source": "unknown",
                       "percent_error": error / actual * 100 if actual > 0 else None})
    errors = [point["absolute_error"] for point in points]
    actual_mean = sum(point["reference"] for point in points) / len(points)
    for point in points:
        point["low_traffic"] = point["reference"] < actual_mean * LOW_TRAFFIC_SHARE
    mae = sum(errors) / len(errors)
    metrics = {"mae": mae, "rmse": math.sqrt(sum(e * e for e in errors) / len(errors)),
               "wape_percent": sum(errors) / sum(point["reference"] for point in points) * 100,
               "actual_mean": actual_mean, "low_traffic_below": actual_mean * LOW_TRAFFIC_SHARE,
               "baseline": {"method": "same_hour_last_week", "lag_hours": 168, "paired_points": hours,
                            "mae": mae / baseline_ratio, "model_mae": mae,
                            "relative_change_percent": (baseline_ratio - 1) * 100}}
    if old_backend:
        metrics = {"mae": metrics["mae"], "rmse": metrics["rmse"]}
        for point in points:
            del point["percent_error"], point["low_traffic"]
    now = end + timedelta(minutes=35)
    return {
        "region_id": REGION, "active_model_version_id": MODEL, "model_version": "model_v7",
        "model_variant": "RandomForest", "dataset_id": "33333333-3333-3333-3333-333333333333",
        "dataset_filename": "synthetic-quality.csv", "timezone": "Asia/Ho_Chi_Minh", "mode": "reference",
        "window": "recent", "reference_source": "dataset_traffic_volume",
        "evaluation_policy": "post_training_one_step_replay_with_dataset_inputs",
        "generated_at": now.isoformat(), "server_time": now.isoformat(),
        "next_refresh_at": (end + timedelta(hours=1)).isoformat(), "selected_hours": hours,
        "start_at": start.isoformat(), "end_at": points[-1]["forecast_for"], "window_end_at": end.isoformat(),
        "available_start_at": start.isoformat(), "available_end_at": points[-1]["forecast_for"],
        "training_end_at": start.isoformat(), "metrics": metrics,
        "coverage": {"requested_points": hours, "dataset_points": hours, "predicted_points": hours,
                     "paired_points": hours, "observed_labels": 0, "synthetic_labels": 0, "unknown_labels": hours},
        "warnings": [], "points": points,
    }


def main():
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    state = {"ratio": 0.79, "old": False}
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
        if url.path.endswith("/model-quality"):
            hours = int(parse_qs(url.query).get("hours", ["168"])[0])
            body = make_report(hours, baseline_ratio=state["ratio"], old_backend=state["old"])
        elif url.path.endswith("/auth/me"):
            body = {"id": "admin", "display_name": "Admin", "email": "admin@example.test", "role": "admin", "is_active": True}
        elif url.path.endswith("/regions"):
            body = {"items": [{"id": REGION, "name": "Synthetic region", "timezone": "Asia/Ho_Chi_Minh", "latitude": 10,
                               "longitude": 106, "is_active": True, "active_model_version_id": MODEL}]}
        elif url.path.endswith("/capabilities"):
            body = {"cloud_training": True, "local_training": True, "local_unavailable_reason": ""}
        elif url.path.endswith("/training-monitor"):
            body = {"items": [], "enabled": True}
        elif url.path.endswith("/drift-settings"):
            body = {"region_id": REGION, "auto_retrain": False}
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
            panel = page.locator(".quality-panel")
            cards = panel.locator(".quality-metrics")
            detail = panel.locator(".quality-point-detail")

            def open_quality():
                page.get_by_role("button", name="Model Quality", exact=True).click()
                expect(page.locator(".topbar h1")).to_have_text("Model Quality")
                expect(cards).to_be_visible()

            def no_overflow(label):
                for width in (1440, 390, 320):
                    page.set_viewport_size({"width": width, "height": 900})
                    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                    assert overflow <= 1, f"{label}: horizontal scroll at {width}px: {overflow}"
                    spill = panel.evaluate("el => [...el.querySelectorAll('.quality-metrics > div, .quality-point-detail > div')]"
                                           ".map(c => c.scrollWidth - c.clientWidth).reduce((a, b) => Math.max(a, b), 0)")
                    assert spill <= 1, f"{label}: a card or detail cell spills {spill}px at {width}px"
                page.set_viewport_size({"width": 1440, "height": 1050})

            def shot(name, width=1440):
                if shots is None:
                    return
                shots.mkdir(parents=True, exist_ok=True)
                # Tall enough that the panel fits without scrolling under the sticky top bar.
                page.set_viewport_size({"width": width, "height": 1400 if width > 600 else 2000})
                page.evaluate("window.scrollTo(0, 0)")
                panel.screenshot(path=str(shots / name))
                page.set_viewport_size({"width": 1440, "height": 1050})

            open_quality()
            expect(cards).to_contain_text("Average error")
            expect(cards).to_contain_text("of actual traffic")
            expect(cards).to_contain_text("vehicles/hour on average")
            expect(cards).to_contain_text("Compared with same hour last week")
            expect(cards).to_contain_text("21% better")
            expect(cards.locator(".quality-metric-worse")).to_have_count(0)
            cases.append("new backend: error share, vehicles and 'better than last week'")

            # Default selection is the last hour, 03:00, a low-traffic hour.
            expect(detail.locator(".quality-tag")).to_have_text("Low traffic")
            expect(detail.locator(".quality-difference-share")).to_contain_text("of actual")
            page.locator(".quality-chart svg").focus()
            for _ in range(12):
                page.keyboard.press("ArrowLeft")  # 15:00, busy hour
            expect(detail.locator(".quality-tag")).to_have_count(0)
            expect(detail.locator(".quality-difference-share")).to_contain_text("of actual")
            cases.append("hour detail: percent of actual; tag only on low-traffic hours")

            panel.get_by_text("View details").click()
            table = panel.locator(".quality-hour-table")
            expect(panel.locator(".quality-report-meta")).to_contain_text("not an accuracy score")
            expect(panel.locator(".quality-report-meta")).to_contain_text("Low traffic")
            expect(table.locator("tbody tr").nth(0)).to_contain_text("%)")
            zero_row = table.locator("tbody tr").nth(22)
            expect(zero_row.locator("td").nth(1)).to_have_text("0")
            assert "%" not in zero_row.locator("td").nth(3).inner_text(), "0-vehicle hour must not show a percent"
            assert table.locator(".quality-tag").count() > 0
            cases.append("details: WAPE explained, table percent column, no percent at 0 vehicles")
            no_overflow("new backend")
            panel.get_by_text("View details").click()
            shot("E2-after-desktop.png")
            shot("E2-after-mobile-390.png", 390)

            state["ratio"] = 1.4
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(cards.locator(".quality-metric-worse")).to_contain_text("40% worse")
            cases.append("model worse than last week is highlighted as a warning")
            shot("E2-after-worse-desktop.png")

            state.update(old=True, ratio=0.79)
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(cards).to_contain_text("Average difference")
            expect(cards).not_to_contain_text("Average error")
            expect(cards).not_to_contain_text("last week")
            expect(detail.locator(".quality-tag")).to_have_count(0)
            expect(detail.locator(".quality-difference-share")).to_have_count(0)
            panel.get_by_text("View details").click()
            assert "%" not in table.locator("tbody tr").nth(0).inner_text()
            panel.get_by_text("View details").click()
            no_overflow("old backend")
            shot("E2-before-desktop.png")
            cases.append("old backend keeps the vehicle-only view without errors")

            for hours, label in ((24, "Last 24 hours"), (336, "Last 14 days")):
                state.update(old=False)
                page.get_by_label("Time range").select_option(label=label)
                expect(panel.locator(".quality-metrics")).to_contain_text("Average error")
                expect(panel.locator(".quality-hour-table tbody tr")).to_have_count(hours)
            cases.append("24 hours and 14 days show the same fields")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
    for case in cases:
        print("PASS", case)
    print(f"{len(cases)} cases passed")


if __name__ == "__main__":
    main()

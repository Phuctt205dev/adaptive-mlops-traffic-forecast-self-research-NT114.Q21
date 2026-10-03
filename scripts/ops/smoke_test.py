import json
import os
import sys
import urllib.error
import urllib.request


API_BASE_URL = os.getenv("SMOKE_API_BASE_URL", "http://traffic-api:8000/api/v1")
ADMIN_EMAIL = os.getenv("SMOKE_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("SMOKE_ADMIN_PASSWORD", "admin123456")


def request(path, method="GET", token=None, payload=None):
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{API_BASE_URL}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            text = response.read().decode("utf-8")
            return response.status, json.loads(text) if text else None
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8")
        detail = json.loads(text) if text else {}
        raise RuntimeError(f"{method} {path} failed: HTTP {error.code} {detail}") from error


def check(name, fn):
    print(f"[smoke] {name}...", end=" ")
    result = fn()
    print("ok")
    return result


def main():
    check("liveness", lambda: request("/health/live"))
    ready_status, ready = check("readiness", lambda: request("/health/ready"))
    if ready_status != 200 or ready.get("status") != "ready":
        raise RuntimeError(f"API is not ready: {ready}")

    _status, login = check(
        "admin login",
        lambda: request(
            "/auth/login",
            method="POST",
            payload={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        ),
    )
    token = login["access_token"]

    _status, summary = check(
        "admin system summary",
        lambda: request("/admin/system/summary", token=token),
    )
    print(
        "[smoke] totals:",
        json.dumps(summary["totals"], ensure_ascii=False, sort_keys=True),
    )

    _status, regions = check(
        "list authenticated regions",
        lambda: request("/regions?page_size=100", token=token),
    )
    active_region = next(
        (
            region
            for region in regions.get("items", [])
            if region.get("active_model_version_id")
        ),
        None,
    )
    if not active_region:
        print("[smoke] prediction skipped: no region has an active model")
        return 0

    _status, prediction = check(
        "prediction",
        lambda: request(
            f"/regions/{active_region['id']}/predictions",
            method="POST",
            token=token,
            payload={
                "forecast_for": "2026-06-19T08:00:00+07:00",
                "humidity": 70,
                "temperature": 28,
                "weather_type": "Clouds",
            },
        ),
    )
    print(
        "[smoke] prediction:",
        round(float(prediction["prediction"]["prediction"]), 2),
        prediction["model_version"],
        prediction["model_variant"],
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"[smoke] failed: {error}", file=sys.stderr)
        raise SystemExit(1)

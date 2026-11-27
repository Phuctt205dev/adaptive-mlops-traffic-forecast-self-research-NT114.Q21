"""E4 step 6.2 — read-only availability over Prometheus retention (7 days), via `kubectl get --raw` service proxy.

Uses kube-state-metrics series already scraped by the cluster (pod readiness, node readiness); no new metrics.
Writes experiments/outputs/e4/availability_7d.json and prints a short table.
Usage: python -X utf8 experiments/scripts/e4_prometheus_availability.py
"""
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments/outputs/e4/availability_7d.json"
TIMELINE = ROOT / "experiments/outputs/e4/api_ready_timeline.csv"
PROXY = "/api/v1/namespaces/monitoring/services/http:traffic-monitoring-prometheus:9090/proxy/api/v1/"
NS = 'namespace="traffic-mlops"'
API = f'sum(kube_deployment_status_replicas_available{{{NS},deployment=~"api|api-next"}})'
WINDOW, STEP = "7d", "1m"


def query(expr):
    raw = subprocess.run(["kubectl", "get", "--raw", PROXY + "query?query=" + quote(expr, safe="")],
                         capture_output=True, text=True, check=True).stdout
    result = json.loads(raw)["data"]["result"]
    return [(r["metric"], float(r["value"][1])) for r in result]


def scalar(expr):
    rows = query(expr)
    return rows[0][1] if rows else None


def main():
    report = {"window": WINDOW, "step": STEP, "source": "prometheus kube-state-metrics (pod/node readiness)"}
    report["api_ready_ge1_share"] = scalar(f"avg_over_time(({API} >= bool 1)[{WINDOW}:{STEP}])")
    report["api_ready_ge2_share"] = scalar(f"avg_over_time(({API} >= bool 2)[{WINDOW}:{STEP}])")
    report["api_ready_min"] = scalar(f"min_over_time({API}[{WINDOW}:{STEP}])")
    report["api_minutes_below2"] = scalar(f"sum_over_time(({API} < bool 2)[{WINDOW}:{STEP}])")
    report["api_minutes_zero"] = scalar(f"sum_over_time(({API} < bool 1)[{WINDOW}:{STEP}])")
    report["minutes_with_data"] = scalar(f"count_over_time({API}[{WINDOW}:{STEP}])")
    report["components"] = {}
    for name in ("postgres", "minio", "mlflow", "airflow-scheduler", "airflow-webserver"):
        sel = f'kube_deployment_status_replicas_available{{{NS},deployment="{name}"}}'
        report["components"][name] = {
            "ready_share": scalar(f"avg_over_time((sum({sel}) >= bool 1)[{WINDOW}:{STEP}])"),
            "minutes_zero": scalar(f"sum_over_time((sum({sel}) < bool 1)[{WINDOW}:{STEP}])")}
    report["nodes"] = {m["node"]: v for m, v in query(
        f'avg_over_time(kube_node_status_condition{{condition="Ready",status="true"}}[{WINDOW}])')}
    # Label by node name (from node_uname_info), so no internal address is written to the output.
    report["node_boot_utc"] = {m.get("nodename"): datetime.fromtimestamp(v, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                               for m, v in query("max by (nodename) (node_boot_time_seconds"
                                                 " * on(instance) group_left(nodename) node_uname_info)")}
    report["restarts_7d"] = {m["pod"]: v for m, v in query(
        f'sum by (pod) (increase(kube_pod_container_status_restarts_total{{{NS}}}[{WINDOW}])) > 0.5')}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    # Timeline of ready API pods (both blue/green slots), one point per minute, for the figure.
    end = int(time.time())
    raw = subprocess.run(["kubectl", "get", "--raw", PROXY + "query_range?query=" + quote(API, safe="")
                          + f"&start={end - 7 * 86400}&end={end}&step=60"],
                         capture_output=True, text=True, check=True).stdout
    values = json.loads(raw)["data"]["result"][0]["values"]
    with TIMELINE.open("w", encoding="utf-8", newline="") as handle:
        handle.write("time_utc,api_ready_pods\n")
        for stamp, value in values:
            handle.write(datetime.fromtimestamp(float(stamp), UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "," + value + "\n")
    print("timeline points", len(values), "->", TIMELINE.relative_to(ROOT))


if __name__ == "__main__":
    main()

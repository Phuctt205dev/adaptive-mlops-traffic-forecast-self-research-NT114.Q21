"""Plan and explicitly apply dashboard-only catalog metadata changes.

The plan never deletes a ConfigMap, workload, datasource, metric, or alert rule.
Three irrelevant provisioned dashboards are disabled at their source, while
other technical dashboards move into a folder. Backups allow scoped rollback.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time

from scripts.observability.grafana_admin import Grafana, digest

REMOVE = {
    "traffic-monitoring-nodes-aix": "Node Exporter / AIX",
    "traffic-monitoring-nodes-darwin": "Node Exporter / MacOS",
    "traffic-monitoring-k8s-resources-multicluster": "Kubernetes / Compute Resources /  Multi-Cluster",
}
PROVIDER = "traffic-monitoring-grafana-config-dashboards"
FOLDER_KEY = "k8s-sidecar-target-directory"
FOLDER_PATH = "/tmp/dashboards/Technical"
CUSTOM = {"traffic-system-overview", "traffic-rke2-nodes"}


def source_dashboard(cm):
    data = cm.get("data", {})
    if len(data) != 1:
        return None
    key, text = next(iter(data.items()))
    if not key.endswith(".json"):
        return None
    try:
        dashboard = json.loads(text)
    except ValueError:
        return None
    return dashboard if isinstance(dashboard, dict) and dashboard.get("uid") and "panels" in dashboard else None


def plan(client, directory):
    destination = directory / "catalog-plan.json"
    if destination.exists():
        raise RuntimeError("Catalog plan already exists; retain backups and use a new directory")
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    sources = client.read("get", "configmaps", "-o", "json")["items"]
    for cm in sources:
        name = cm["metadata"]["name"]
        if name == PROVIDER:
            before = cm["data"]["provider.yaml"]
            if ("foldersFromFilesStructure: false" not in before or "disableDeletion: false" not in before
                    or "path: /tmp/dashboards" not in before or "folder: ''" not in before):
                raise RuntimeError("Dashboard provider differs from the reviewed layout")
            patch = [{"op": "test", "path": "/data/provider.yaml", "value": before},
                     {"op": "replace", "path": "/data/provider.yaml", "value": before.replace("foldersFromFilesStructure: false", "foldersFromFilesStructure: true")}]
            operation = "enable-folders"
            title, uid = "Dashboard file folders", None
        else:
            if cm["metadata"].get("labels", {}).get("grafana_dashboard") != "1":
                continue
            dashboard = source_dashboard(cm)
            if dashboard is None or dashboard["uid"] in CUSTOM:
                raise RuntimeError("Unrecognized provisioned dashboard source; manual review required")
            title, uid = dashboard["title"], dashboard["uid"]
            if name in REMOVE:
                if title != REMOVE[name]:
                    raise RuntimeError("Removal target title differs")
                operation = "disable-dashboard"
                patch = [{"op": "test", "path": "/metadata/labels/grafana_dashboard", "value": "1"},
                         {"op": "replace", "path": "/metadata/labels/grafana_dashboard", "value": "0"}]
            else:
                operation = "move-to-technical"
                patch = []
                if "annotations" not in cm["metadata"]:
                    patch.append({"op": "add", "path": "/metadata/annotations", "value": {}})
                patch.append({"op": "add", "path": "/metadata/annotations/" + FOLDER_KEY, "value": FOLDER_PATH})
        guard = [{"op": "test", "path": "/metadata/uid", "value": cm["metadata"]["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": cm["metadata"]["resourceVersion"]}]
        backup = "catalog-before-" + name + ".json"
        (directory / backup).write_text(json.dumps(cm, indent=2), encoding="utf-8")
        entries.append({"name": name, "uid": uid, "title": title, "operation": operation,
            "before_sha256": digest(cm), "backup": backup, "patch": guard + patch})
    if ({item["name"] for item in entries if item["operation"] == "disable-dashboard"} != set(REMOVE)
            or sum(item["operation"] == "enable-folders" for item in entries) != 1):
        raise RuntimeError("Expected three removal targets and one dashboard provider")
    result = {"schema": 1, "namespace": "monitoring", "technical_folder": "Technical", "entries": entries}
    (directory / "catalog-plan.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"plan": str(destination), "changes": [{key: item[key] for key in ("name", "title", "operation")} for item in entries]}))


def reviewed(directory):
    review = json.loads((directory / "catalog-plan.json").read_text())
    if review.get("schema") != 1 or review.get("namespace") != "monitoring":
        raise RuntimeError("Unknown catalog plan")
    for entry in review["entries"]:
        if not entry["name"].startswith("traffic-monitoring-") or entry["operation"] not in {
                "enable-folders", "disable-dashboard", "move-to-technical"}:
            raise RuntimeError("Catalog operation outside reviewed scope")
        allowed = {"/metadata/uid", "/metadata/resourceVersion"}
        if entry["operation"] == "enable-folders":
            if entry["name"] != PROVIDER:
                raise RuntimeError("Only the known dashboard provider can be updated")
            allowed.add("/data/provider.yaml")
        elif entry["operation"] == "disable-dashboard":
            if entry["name"] not in REMOVE or entry["title"] != REMOVE[entry["name"]]:
                raise RuntimeError("Only the three reviewed dashboards may be disabled")
            allowed.add("/metadata/labels/grafana_dashboard")
        else:
            if entry["name"] in REMOVE or entry["uid"] in CUSTOM:
                raise RuntimeError("Admin dashboards cannot be moved by this plan")
            allowed.update({"/metadata/annotations", "/metadata/annotations/" + FOLDER_KEY})
        if any(item["path"] not in allowed or item["op"] not in {"test", "add", "replace"} for item in entry["patch"]):
            raise RuntimeError("Unexpected field in dashboard catalog patch")
    return review


def mutate(client, name, patch, *, dry_run=False):
    args = [*client.base, "patch", "configmap", name, "--type=json", "-p", json.dumps(patch), "-o", "name"]
    if dry_run:
        args.append("--dry-run=server")
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Dashboard ConfigMap patch rejected; no raw output retained")


def preflight(client, review):
    for entry in review["entries"]:
        current = client.read("get", "configmap", entry["name"], "-o", "json")
        if digest(current) != entry["before_sha256"]:
            raise RuntimeError("Catalog source changed since plan: " + entry["name"])


def dry_run(client, directory):
    review = reviewed(directory)
    preflight(client, review)
    for entry in review["entries"]:
        mutate(client, entry["name"], entry["patch"], dry_run=True)
    print(json.dumps({"server_dry_run": "passed", "configmaps": len(review["entries"]), "persisted_changes": False}))


def apply(client, directory):
    review = reviewed(directory)
    preflight(client, review)
    completed = []
    for entry in sorted(review["entries"], key=lambda e: e["operation"] != "enable-folders"):
        mutate(client, entry["name"], entry["patch"])
        completed.append(entry["name"])
        (directory / "catalog-applied.json").write_text(json.dumps(completed, indent=2), encoding="utf-8")
        print(json.dumps({"patched_dashboard_source": entry["name"]}), flush=True)
    print(json.dumps({"applied": len(completed), "next": "verify"}), flush=True)


def verify(client, directory):
    review = reviewed(directory)
    removed = {e["uid"] for e in review["entries"] if e["operation"] == "disable-dashboard"}
    moved = {e["uid"] for e in review["entries"] if e["operation"] == "move-to-technical"}
    # The ConfigMap mount may take time to refresh. Reload only Grafana's
    # dashboard provider; do not restart pods or touch Prometheus configuration.
    deadline = time.monotonic() + 180
    while True:
        client.requests([{"method": "POST", "path": "/api/admin/provisioning/dashboards/reload", "body": {}}])
        items = client.get("/api/search?type=dash-db&limit=1000")
        lookup = {item["uid"]: item for item in items}
        passed = not (set(lookup) & removed) and moved.issubset(lookup) and all(
            lookup[uid].get("folderTitle") == "Technical" for uid in moved) and CUSTOM.issubset(lookup)
        if passed or time.monotonic() >= deadline:
            result = {"passed": passed, "removed": len(removed - set(lookup)), "technical": sum(
                item.get("folderTitle") == "Technical" for item in items), "total": len(items),
                "root_dashboards": [item["title"] for item in items if not item.get("folderUid")]}
            (directory / "catalog-verify.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result), flush=True)
            if not passed:
                raise RuntimeError("Catalog not yet verified; inspect before retry or rollback")
            return
        print(json.dumps({"waiting_for_dashboard_provisioning": True}), flush=True)
        time.sleep(10)


def rollback(client, directory):
    review = reviewed(directory)
    for entry in review["entries"]:
        before = json.loads((directory / entry["backup"]).read_text())
        current = client.read("get", "configmap", entry["name"], "-o", "json")
        if current["metadata"]["uid"] != before["metadata"]["uid"]:
            raise RuntimeError("Catalog object identity changed")
        guard = [{"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]}]
        if entry["operation"] == "enable-folders":
            desired = before["data"]["provider.yaml"].replace("foldersFromFilesStructure: false", "foldersFromFilesStructure: true")
            patch = [{"op": "test", "path": "/data/provider.yaml", "value": desired},
                     {"op": "replace", "path": "/data/provider.yaml", "value": before["data"]["provider.yaml"]}]
        elif entry["operation"] == "disable-dashboard":
            if current["data"] != before["data"]:
                raise RuntimeError("Disabled source changed; rollback refused")
            patch = [{"op": "test", "path": "/metadata/labels/grafana_dashboard", "value": "0"},
                     {"op": "replace", "path": "/metadata/labels/grafana_dashboard", "value": "1"}]
        else:
            path = "/metadata/annotations/" + FOLDER_KEY
            patch = [{"op": "test", "path": path, "value": FOLDER_PATH}]
            old = before["metadata"].get("annotations", {}).get(FOLDER_KEY)
            patch.append({"op": "add", "path": path, "value": old} if old is not None else {"op": "remove", "path": path})
        mutate(client, entry["name"], guard + patch)
    client.requests([{"method": "POST", "path": "/api/admin/provisioning/dashboards/reload", "body": {}}])
    print(json.dumps({"rolled_back_dashboard_sources": len(review["entries"]), "verify_after_mount_refresh": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "dry-run", "apply", "verify", "rollback"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--kubeconfig", default=str(Path.home() / ".kube/config"))
    parser.add_argument("--context", default="default")
    args = parser.parse_args()
    client = Grafana(args.kubectl, args.kubeconfig, args.context)
    {"plan": plan, "dry-run": dry_run, "apply": apply, "verify": verify, "rollback": rollback}[args.action](client, args.directory)


if __name__ == "__main__":
    main()

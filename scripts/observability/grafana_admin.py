"""Review/update the two admin dashboards through Grafana; never print credentials.

Grafana authentication is read inside its existing sidecar. Kubernetes objects
are read only. Provisioned dashboard catalog changes require a separate plan.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
UID_FILES = {"traffic-system-overview": "system-overview.json", "traffic-rke2-nodes": "nodes.json"}
REMOTE = '''
import base64,json,os
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError
user,password=os.environ.get('REQ_USERNAME'),os.environ.get('REQ_PASSWORD')
if not user or not password:raise RuntimeError('Sidecar Grafana auth is unavailable')
authorization='Basic '+base64.b64encode((user+':'+password).encode()).decode()
opener=build_opener(ProxyHandler({}))
results=[]
for item in REQUESTS:
    request=Request('http://127.0.0.1:3000'+item['path'],method=item.get('method','GET'),
        data=json.dumps(item['body']).encode() if 'body' in item else None,
        headers={'Authorization':authorization,'Content-Type':'application/json'})
    try:
        with opener.open(request,timeout=20) as response:
            results.append({'status':response.status,'body':json.load(response)})
    except HTTPError as error:
        results.append({'status':error.code,'error':'Grafana request rejected'})
        break
    except Exception as error:
        results.append({'status':0,'error_type':type(error).__name__})
        break
print(json.dumps(results))
'''


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Grafana:
    def __init__(self, kubectl, kubeconfig, context):
        self.base = [kubectl, "--kubeconfig", kubeconfig, "--context", context,
                     "--request-timeout=20s", "-n", "monitoring"]
        pods = self.read("get", "pods", "-o", "json")["items"]
        candidates = [p for p in pods if p["status"].get("phase") == "Running"
            and {"grafana", "grafana-sc-dashboard"}.issubset({c["name"] for c in p["spec"]["containers"]})]
        if len(candidates) != 1:
            raise RuntimeError("Expected exactly one running Grafana pod")
        self.pod = candidates[0]["metadata"]["name"]

    def read(self, *arguments):
        result = subprocess.run([*self.base, *arguments], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Kubernetes read failed; raw output omitted")
        return json.loads(result.stdout)

    def requests(self, requests):
        source = "REQUESTS=" + repr(requests) + "\n" + REMOTE
        result = subprocess.run([*self.base, "exec", "-i", self.pod, "-c", "grafana-sc-dashboard", "--", "python", "-"],
            input=source, capture_output=True, text=True, timeout=max(40, len(requests) * 22))
        if result.returncode:
            raise RuntimeError("Grafana request failed; raw output omitted")
        return json.loads(result.stdout)

    def get(self, path):
        result = self.requests([{"path": path}])[0]
        if result["status"] != 200:
            raise RuntimeError("Grafana read rejected with HTTP " + str(result["status"]))
        return result["body"]


def query_map(dashboard):
    def walk(panels):
        for panel in panels:
            yield panel
            yield from walk(panel.get("panels", []))
    return {str(p["id"]) + ":" + target["refId"]: target["expr"]
            for p in walk(dashboard["panels"]) for target in p.get("targets", [])}


def plan(client, directory):
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "dashboard-plan.json"
    if destination.exists():
        raise RuntimeError("Plan already exists; use a new directory to retain the backup")
    entries = []
    for uid, filename in UID_FILES.items():
        desired = json.loads((ROOT / "deployment/observability/dashboards" / filename).read_text(encoding="utf-8"))
        current = client.get("/api/dashboards/uid/" + uid)
        if current["meta"].get("provisioned") or desired["uid"] != uid:
            raise RuntimeError("Only the two editable admin dashboards may be updated")
        (directory / ("before-" + uid + ".json")).write_text(json.dumps(current, indent=2), encoding="utf-8")
        entries.append({"uid": uid, "filename": filename, "before_sha256": digest(current["dashboard"]),
            "desired_sha256": digest(desired), "version": current["dashboard"]["version"],
            "id": current["dashboard"]["id"], "folder_uid": current["meta"].get("folderUid"),
            "old_panels": len(current["dashboard"]["panels"]), "new_panels": len(desired["panels"])})
    result = {"schema": 1, "namespace": "monitoring", "entries": entries}
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


def apply(client, directory):
    review = json.loads((directory / "dashboard-plan.json").read_text())
    if (review.get("schema") != 1 or review.get("namespace") != "monitoring"
            or len(review.get("entries", [])) != 2
            or {item.get("uid"): item.get("filename") for item in review["entries"]} != UID_FILES):
        raise RuntimeError("Only the two reviewed admin dashboards may be updated")
    requests = []
    for entry in review["entries"]:
        desired = json.loads((ROOT / "deployment/observability/dashboards" / entry["filename"]).read_text(encoding="utf-8"))
        current = client.get("/api/dashboards/uid/" + entry["uid"])
        if (digest(current["dashboard"]) != entry["before_sha256"] or digest(desired) != entry["desired_sha256"]
                or current["meta"].get("provisioned") or desired["uid"] not in UID_FILES):
            raise RuntimeError("Source or dashboard changed after review; update refused")
        desired.update(id=entry["id"], version=entry["version"])
        body = {"dashboard": desired, "overwrite": False, "message": "Simplify admin monitoring; retain missing-data and readiness checks"}
        if entry.get("folder_uid"):
            body["folderUid"] = entry["folder_uid"]
        requests.append({"method": "POST", "path": "/api/dashboards/db", "body": body})
    results = client.requests(requests)
    (directory / "dashboard-apply.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results))
    if len(results) != 2 or any(r["status"] != 200 or r["body"].get("status") != "success" for r in results):
        raise RuntimeError("Dashboard update incomplete; inspect results and backups before retry")


def verify(client, directory):
    proof = []
    for uid, filename in UID_FILES.items():
        desired = json.loads((ROOT / "deployment/observability/dashboards" / filename).read_text(encoding="utf-8"))
        current = client.get("/api/dashboards/uid/" + uid)["dashboard"]
        valid = (current["uid"] == desired["uid"] and current["title"] == desired["title"]
                 and current["panels"] == desired["panels"] and query_map(current) == query_map(desired))
        proof.append({"uid": uid, "version": current["version"], "panels_and_queries_match": valid})
    (directory / "dashboard-verify.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps(proof))
    if not all(p["panels_and_queries_match"] for p in proof):
        raise RuntimeError("Grafana saved definition differs; review before continuing")


def rollback(client, directory):
    requests = []
    for uid, filename in UID_FILES.items():
        before = json.loads((directory / ("before-" + uid + ".json")).read_text())
        current = client.get("/api/dashboards/uid/" + uid)
        desired = json.loads((ROOT / "deployment/observability/dashboards" / filename).read_text(encoding="utf-8"))
        if (current["meta"].get("provisioned") or current["dashboard"]["panels"] != desired["panels"]
                or current["dashboard"]["title"] != desired["title"]):
            raise RuntimeError("Dashboard drifted; rollback refused")
        restored = copy.deepcopy(before["dashboard"])
        restored["version"] = current["dashboard"]["version"]
        body = {"dashboard": restored, "overwrite": False, "message": "Restore reviewed admin dashboard backup"}
        if before["meta"].get("folderUid"):
            body["folderUid"] = before["meta"]["folderUid"]
        requests.append({"method": "POST", "path": "/api/dashboards/db", "body": body})
    results = client.requests(requests)
    print(json.dumps(results))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "verify", "rollback"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--kubeconfig", default=str(Path.home() / ".kube/config"))
    parser.add_argument("--context", default="default")
    args = parser.parse_args()
    client = Grafana(args.kubectl, args.kubeconfig, args.context)
    globals()[args.action](client, args.directory)


if __name__ == "__main__":
    main()

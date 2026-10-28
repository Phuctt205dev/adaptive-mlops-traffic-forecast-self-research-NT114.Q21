"""Read-only RKE2 snapshot for review. Never write raw Deployment/Secret JSON."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

NAMES = ("traffic-api", "traffic-training-tree", "traffic-training-worker", "airflow-scheduler", "airflow-webserver")
SAFE_ENV = {"TRAFFIC_CLOUD_MODE", "TRAINING_CONTROL_ENABLED", "CLOUD_TRAINING_ENABLED", "KAGGLE_USERNAME",
            "KAGGLE_PYTHON", "KAGGLE_TOKEN_FILE", "TRAFFIC_TRAINING_ROLE", "TRAFFIC_SHAP_MODE", "TRAFFIC_TRAINING_MODE"}
PROBE = '''import json,sys,hashlib,importlib.metadata as m
from pathlib import Path
from urllib.request import urlopen
result={"python":sys.version.split()[0],"packages":{}}
for name in ["numpy","pandas","scikit-learn","joblib","xgboost","lightgbm","tensorflow","mlflow"]:
 try:result["packages"][name]=m.version(name)
 except m.PackageNotFoundError:result["packages"][name]=None
result["sources"]={}
for value in ["/opt/traffic-shap/shap_system_runtime.py","/opt/traffic-training/training_worker_execution.py"]:
 p=Path(value)
 if p.is_file():result["sources"][value]=hashlib.sha256(p.read_bytes()).hexdigest()
try:
 with urlopen("http://127.0.0.1:8000/health/integration",timeout=3) as r:result["integration"]=json.load(r)
except Exception:pass
print(json.dumps(result))
'''
DB_PROBE = '''import json
from sqlalchemy import text
from backend.app.db.session import SessionLocal
with SessionLocal() as db:
 head=db.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
 count=db.execute(text("SELECT count(*) FROM training_runs WHERE status IN ('queued','training')")).scalar()
 print(json.dumps({"migration_heads":head,"open_training_runs":count}))
'''


def run(kubectl, *arguments, stdin=None):
    result = subprocess.run([kubectl, "--request-timeout=20s", "-n", "traffic-mlops", *arguments],
        input=stdin, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=50)
    if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
        raise RuntimeError("A read-only cluster query failed; raw response omitted.")
    return json.loads(result.stdout)


def capture(kubectl):
    document = {"checked_at": datetime.now(timezone.utc).isoformat(), "namespace": "traffic-mlops", "deployments": {}}
    for name in NAMES:
        raw = run(kubectl, "get", "deployment", name, "-o", "json")
        pod = raw["spec"]["template"]["spec"]
        containers = []
        for entry in pod["containers"]:
            containers.append({"name": entry["name"], "image": entry["image"], "command": entry.get("command"),
                "args": entry.get("args"), "env_names": [item["name"] for item in entry.get("env", [])],
                "safe_env": [item for item in entry.get("env", []) if item["name"] in SAFE_ENV],
                "envFrom": entry.get("envFrom", []),
                "reference_env": [item for item in entry.get("env", []) if "valueFrom" in item],
                "volumeMounts": entry.get("volumeMounts", []), "ports": entry.get("ports", [])})
        document["deployments"][name] = {"uid": raw["metadata"]["uid"], "resourceVersion": raw["metadata"]["resourceVersion"],
            "generation": raw["metadata"]["generation"], "replicas": raw["spec"].get("replicas", 1),
            "selector": raw["spec"]["selector"], "containers": containers, "volumes": pod.get("volumes", []),
            "init_names": [item["name"] for item in pod.get("initContainers", [])], "nodeSelector": pod.get("nodeSelector", {})}
    pods = run(kubectl, "get", "pods", "-o", "json")["items"]
    document["runtime"] = {}
    for name in NAMES[:3]:
        candidates = [entry for entry in pods if entry["metadata"]["name"].startswith(name + "-")
                      and entry["status"]["phase"] == "Running" and not entry["metadata"].get("deletionTimestamp")]
        if not candidates:
            raise RuntimeError("A required runtime pod is not running.")
        pod_name = candidates[0]["metadata"]["name"]
        main = next(entry for entry in document["deployments"][name]["containers"]
                    if any(port.get("containerPort") == 8000 for port in entry["ports"]))
        report = run(kubectl, "exec", "-i", pod_name, "-c", main["name"], "--", "python", "-B", "-c", PROBE)
        report.update(pod=pod_name, node=candidates[0]["spec"]["nodeName"], main_container=main["name"])
        document["runtime"][name] = report
        if name == "traffic-api":
            document["database"] = run(kubectl, "exec", "-i", pod_name, "-c", main["name"], "--", "python", "-B", "-c", DB_PROBE)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        document = capture(args.kubectl)
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, ensure_ascii=False)
        print(json.dumps({"read_only_snapshot": str(path), "deployments": list(document["deployments"]), **document["database"]}))
        return 0
    except Exception as error:
        print(json.dumps({"snapshot_failed": True, "error_type": type(error).__name__, "raw_details_omitted": True}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

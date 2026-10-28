"""Generate an immutable source overlay and narrow rollout/rollback patches.

Offline only. This command never calls kubectl, migrates a DB or enables cloud.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.cloud_training.package import ml_sources
BACKEND = ("core/config.py", "schemas/model_version.py", "api/routes/datasets.py", "api/routes/training_monitor.py",
           "db/training_execution.py", "services/model_registry.py", "services/training_execution.py",
           "services/training_control.py", "services/training_tracking.py")
CLOUD = ("__init__.py", "probe.py", "artifacts.py", "package.py", "process.py", "control.py", "notebook.py",
         "kaggle_rpc.py", "kaggle_transport.py", "kaggle_execution.py")
SDK_LOCK = """attrs==26.1.0
bleach==6.4.0
certifi==2026.10.23
charset-normalizer==3.5.2
colorama==0.4.6
fastjsonschema==2.22.2
idna==3.20
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
jupyter_core==5.9.1
jupytext==1.19.6
kaggle==2.2.5
kagglesdk==0.1.38
markdown-it-py==4.2.0
mdit-py-plugins==0.6.1
mdurl==0.1.2
nbformat==5.11.1
packaging==26.3
platformdirs==4.13.7
protobuf==7.36.2
python-dateutil==3.0.0
python-dotenv==1.2.4
python-slugify==9.1.3
PyYAML==6.0.3
referencing==0.37.0
requests==2.34.2
rpds-py==2026.9.1
six==1.17.0
text-unidecode==1.3
tqdm==4.70.1
traitlets==5.16.1
typing_extensions==4.16.0
tzdata==2026.5
urllib3==2.8.0
webencodings==0.6.1
"""


def module_name(name):
    value = name.removesuffix(".py").replace("/", ".")
    return value.removesuffix(".__init__")


def sources():
    files = {"project/" + name: data for name, data in ml_sources(ROOT).items()}
    for name in CLOUD:
        files["project/src/cloud_training/" + name] = (ROOT / "src/cloud_training" / name).read_bytes()
    for name in BACKEND:
        files["project/backend/app/" + name] = (ROOT / "backend/app" / name).read_bytes()
    for name in ("execute_branch.py", "verify_execution.py"):
        files["project/scripts/cloud_training/" + name] = (ROOT / "scripts/cloud_training" / name).read_bytes()
    files["cloud_training_runtime.py"] = (ROOT / "Deployment_test_on_rke2/runtime/cloud_training_runtime.py").read_bytes()
    files["migration.py"] = (ROOT / "alembic/versions/20261028_01_training_executions.py").read_bytes()
    files["run_migration.py"] = (ROOT / "scripts/cloud_training/run_migration.py").read_bytes()
    files["sdk-requirements.txt"] = SDK_LOCK.encode()
    return files


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def code_map(files):
    manifest = {"revision": "cloud-training-v1", "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                "modules": {module_name(name.removeprefix("project/")): name for name in files
                            if name.startswith(("project/src/", "project/backend/")) and name.endswith(".py")}}
    manifest["bundle_id"] = hashlib.sha256(canonical(manifest)).hexdigest()
    files = {**files, "manifest.json": canonical(manifest)}
    data, items = {}, []
    for index, (name, content) in enumerate(sorted(files.items())):
        key = f"f{index:03}"
        data[key] = content.decode("utf-8")
        items.append({"key": key, "path": name})
    resource = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": "traffic-cloud-" + manifest["bundle_id"][:12], "namespace": "traffic-mlops"}, "immutable": True, "data": data}
    if len(canonical(resource)) > 900 * 1024:
        raise ValueError("Cloud source ConfigMap exceeds its reviewed size budget.")
    return resource, items, manifest


def main_container(snapshot, name):
    entries = snapshot["deployments"][name]["containers"]
    if name.startswith("airflow"):
        return next(entry for entry in entries if entry["name"] == name)
    return next(entry for entry in entries if any(port.get("containerPort") == 8000 for port in entry["ports"]))


def patch_for(snapshot, name, code, items, airflow, *, rollback=False):
    current = main_container(snapshot, name)
    worker = name in {"traffic-training-tree", "traffic-training-worker"}
    mode = "worker" if worker else "api"
    container = {"name": current["name"]}
    if name.startswith("airflow"):
        target = "/opt/airflow/dags/traffic_region_training.py"
        prior = next(item for item in current["volumeMounts"] if item["mountPath"] == target)
        mounts = [{"name": "cloud-airflow", "mountPath": "/opt/traffic-cloud", "readOnly": True},
                  {"name": "cloud-airflow", "mountPath": target, "subPath": "dag.py", "readOnly": True}]
        if rollback:
            mounts = [{"mountPath": "/opt/traffic-cloud", "$patch": "delete"}, prior]
        container["volumeMounts"] = mounts
        volumes = [{"name": "cloud-airflow", "$patch": "delete"}] if rollback else [
            {"name": "cloud-airflow", "configMap": {"name": airflow["metadata"]["name"], "items": [
                {"key": "dag.py", "path": "dag.py"}, {"key": "airflow_control.py", "path": "airflow_control.py"},
                {"key": "legacy.py", "path": "legacy/traffic_region_training_worker.py"}]}}]
        return {"spec": {"template": {"spec": {"containers": [container], "volumes": volumes}}}}
    flags = {"TRAFFIC_CLOUD_MODE": mode, "TRAINING_CONTROL_ENABLED": "false", "CLOUD_TRAINING_ENABLED": "false"}
    if worker:
        flags.update(KAGGLE_USERNAME="truongphucdev", KAGGLE_PYTHON="/opt/traffic-kaggle-sdk/bin/python",
                     KAGGLE_TOKEN_FILE="/var/run/traffic-kaggle/access_token")
    if rollback:
        original = {entry["name"]: entry for entry in current["safe_env"]}
        container.update(command=current["command"], args=current["args"],
            env=[original.get(key, {"name": key, "$patch": "delete"}) for key in flags],
            volumeMounts=[{"mountPath": path, "$patch": "delete"} for path in (
                "/opt/traffic-cloud", *(("/opt/traffic-kaggle-sdk", "/var/run/traffic-kaggle") if worker else ()))])
        volumes = [{"name": key, "$patch": "delete"} for key in (
            "cloud-code", *(("cloud-sdk", "kaggle-credential") if worker else ()))]
        pod = {"containers": [container], "volumes": volumes}
        if worker:
            pod["initContainers"] = [{"name": "install-cloud-sdk", "$patch": "delete"}]
    else:
        container.update(command=["python", "-B", "/opt/traffic-cloud/cloud_training_runtime.py"], args=[],
            env=[{"name": key, "value": value} for key, value in flags.items()],
            volumeMounts=[{"name": "cloud-code", "mountPath": "/opt/traffic-cloud", "readOnly": True}])
        volumes = [{"name": "cloud-code", "configMap": {"name": code["metadata"]["name"], "items": items}}]
        pod = {"containers": [container], "volumes": volumes}
        if worker:
            if "install-cloud-sdk" in snapshot["deployments"][name]["init_names"]:
                raise ValueError("An SDK init container already exists; capture/review that revision first.")
            volumes.extend([{"name": "cloud-sdk", "emptyDir": {"sizeLimit": "512Mi"}},
                            {"name": "kaggle-credential", "secret": {"secretName": "traffic-kaggle-credentials", "defaultMode": 256}}])
            container["volumeMounts"].extend([
                {"name": "cloud-sdk", "mountPath": "/opt/traffic-kaggle-sdk", "readOnly": True},
                {"name": "kaggle-credential", "mountPath": "/var/run/traffic-kaggle", "readOnly": True}])
            install = "import venv,subprocess;venv.create('/opt/traffic-kaggle-sdk',with_pip=True);subprocess.run(['/opt/traffic-kaggle-sdk/bin/python','-m','pip','--isolated','install','--no-cache-dir','--only-binary=:all:','--disable-pip-version-check','--timeout','15','--retries','1','-r','/opt/traffic-cloud/sdk-requirements.txt'],check=True,timeout=240)"
            pod["initContainers"] = [{"name": "install-cloud-sdk", "image": current["image"],
                "command": ["python", "-B", "-c", install], "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "1", "memory": "512Mi"}}, "volumeMounts": [
                    {"name": "cloud-code", "mountPath": "/opt/traffic-cloud", "readOnly": True},
                    {"name": "cloud-sdk", "mountPath": "/opt/traffic-kaggle-sdk"}]}]
    return {"spec": {"template": {"spec": pod}}}


def prepare(snapshot, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    code, items, manifest = code_map(sources())
    airflow_data = {"dag.py": (ROOT / "Deployment_test_on_rke2/runtime/traffic_region_training_cloud.py").read_text(encoding="utf-8"),
                    "legacy.py": (ROOT / "Deployment_test_on_rke2/runtime/traffic_region_training_worker.py").read_text(encoding="utf-8"),
                    "airflow_control.py": (ROOT / "src/cloud_training/airflow_control.py").read_text(encoding="utf-8")}
    airflow = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "traffic-cloud-airflow-" +
        hashlib.sha256(canonical(airflow_data)).hexdigest()[:12], "namespace": "traffic-mlops"}, "immutable": True, "data": airflow_data}
    resources = {"configmaps.json": {"apiVersion": "v1", "kind": "List", "items": [code, airflow]}}
    for name in snapshot["deployments"]:
        resources[name + ".patch.json"] = patch_for(snapshot, name, code, items, airflow)
        resources[name + ".rollback.json"] = patch_for(snapshot, name, code, items, airflow, rollback=True)
    api = main_container(snapshot, "traffic-api")
    resources["migration-job.json"] = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {
        "name": "traffic-cloud-migrate-" + manifest["bundle_id"][:10], "namespace": "traffic-mlops"}, "spec": {
        "backoffLimit": 0, "activeDeadlineSeconds": 180, "template": {"spec": {"restartPolicy": "Never", "containers": [{
            "name": "migrate", "image": api["image"], "command": ["python", "-B", "/opt/traffic-cloud/run_migration.py"],
            "envFrom": api.get("envFrom", []), "env": api.get("reference_env", []),
            "volumeMounts": [{"name": "cloud-code", "mountPath": "/opt/traffic-cloud", "readOnly": True}]}],
            "volumes": [{"name": "cloud-code", "configMap": {"name": code["metadata"]["name"], "items": items}}]}}}}
    summary = {"bundle_id": manifest["bundle_id"], "revision": manifest["revision"], "review_only": True,
               "cloud_enabled": False, "snapshot_checked_at": snapshot["checked_at"], "database": snapshot["database"],
               "legacy_dag_sha256": hashlib.sha256(airflow_data["legacy.py"].encode()).hexdigest(),
               "sdk_lock_sha256": hashlib.sha256(SDK_LOCK.encode()).hexdigest(),
               "files": {}}
    for name, data in resources.items():
        raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        (directory / name).write_bytes(raw)
        summary["files"][name] = hashlib.sha256(raw).hexdigest()
    (directory / "review.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = prepare(json.loads(Path(args.snapshot).read_text(encoding="utf-8")), args.output)
    print(json.dumps({key: result[key] for key in ("bundle_id", "review_only", "cloud_enabled")}))


if __name__ == "__main__":
    main()

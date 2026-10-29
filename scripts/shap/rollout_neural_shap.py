"""Prepare/Check are read-only; Apply/Verify/Rollback are explicit operator actions.

Update only the cloud source in API/two workers and add the already reviewed
SHAP dependency installer to the recurrent worker. Airflow/image/flags/DB stay.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.cloud_training.prepare_rollout import canonical, code_map, sources
from scripts.cloud_training.manage_rollout import Rollout, READINESS
from scripts.cloud_training.snapshot_cluster import capture

PRIOR = ROOT / "results/cloud-training/cloud-monitor-fix"
ROLES = ("traffic-training-tree", "traffic-training-worker", "traffic-api")
ALLOWED = {"project/src/shap_reporting.py", "project/src/time_series_cross_validation.py",
           "project/scripts/cloud_training/execute_branch.py", "project/backend/app/services/training_control.py",
           "project/backend/app/services/feature_importance.py", "project/backend/app/schemas/feature_importance.py",
           "cloud_training_runtime.py"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_manifest(code):
    return next(json.loads(value) for value in code["data"].values() if value.startswith('{"bundle_id":'))


def neural_patch(tree, neural, code, items):
    """Borrow the exact tree installer; preserve every existing recurrent mount."""
    tree_pod, neural_pod = tree["spec"]["template"]["spec"], neural["spec"]["template"]["spec"]
    tree_main = next(value for value in tree_pod["containers"] if value["name"] == "training-worker")
    main = next(value for value in neural_pod["containers"] if value["name"] == "training-worker")
    volumes = [copy.deepcopy(value) for value in tree_pod["volumes"] if value["name"] in {"shap-runtime", "shap-dependencies"}]
    mounts = [copy.deepcopy(value) for value in tree_main["volumeMounts"] if value["name"] in {"shap-runtime", "shap-dependencies"}]
    installer = copy.deepcopy(next(value for value in tree_pod["initContainers"] if value["name"] == "install-shap"))
    if len(volumes) != 2 or len(mounts) != 2 or installer["image"] != main["image"]:
        raise RuntimeError("The existing SHAP dependency installer differs from the neural image.")
    if any(value["name"] in {"shap-runtime", "shap-dependencies"} for value in neural_pod["volumes"]):
        raise RuntimeError("Neural SHAP is already mounted; prepare an upgrade against that revision instead.")
    for variable in installer.get("env", []):
        if "value" in variable and any(word in variable["name"] for word in ("PASSWORD", "TOKEN", "SECRET", "ACCESS_KEY")):
            raise RuntimeError("Do not persist literal credentials in an installer patch.")
    volumes.append({"name": "cloud-code", "configMap": {"name": code["metadata"]["name"], "items": items}})
    return {"spec": {"template": {"spec": {"volumes": volumes, "initContainers": [installer],
        "containers": [{"name": main["name"], "volumeMounts": mounts,
                        "env": [{"name": "NUMBA_NUM_THREADS", "value": "1"},
                                {"name": "NUMBA_CACHE_DIR", "value": "/tmp/traffic-shap-numba"}]}]}}}}


def prepare(kubectl, directory):
    directory = Path(directory)
    if directory.exists():
        raise RuntimeError("Keep an existing review unchanged; use a new output directory.")
    prior_resources = read(PRIOR / "configmaps.json")["items"]
    previous = next(item for item in prior_resources if item["metadata"]["name"].startswith("traffic-cloud-")
                    and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    old = code_manifest(previous)
    code, items, manifest = code_map(sources())
    changed = {name for name in set(old["files"]) | set(manifest["files"]) if old["files"].get(name) != manifest["files"].get(name)}
    if changed != ALLOWED:
        raise RuntimeError("Source scope differs from the neural SHAP review.")
    baseline = capture(kubectl)
    cluster = Rollout(kubectl, PRIOR, PRIOR / "baseline.json")
    cluster.verify(expected=True, bundle_id=old["bundle_id"])
    tree = cluster.call("get", "deployment", ROLES[0], "-o", "json", json_result=True)
    neural = cluster.call("get", "deployment", ROLES[1], "-o", "json", json_result=True)
    dependency_volume = next(value for value in tree["spec"]["template"]["spec"]["volumes"] if value["name"] == "shap-runtime")
    dependency_config = cluster.call("get", "configmap", dependency_volume["configMap"]["name"], "-o", "json", json_result=True)
    # Store code/data only, not server managedFields or unrelated metadata.
    dependency_config = {"data": dependency_config["data"], "name": dependency_volume["configMap"]["name"]}
    resources = [code, *[item for item in prior_resources if item["metadata"]["name"].startswith("traffic-cloud-airflow-")]]
    documents = {"baseline.json": baseline, "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": resources},
                 "shap-dependency-config.json": dependency_config}
    for name in ROLES:
        prior = next(value for value in baseline["deployments"][name]["volumes"] if value["name"] == "cloud-code")
        if prior["configMap"]["name"] != previous["metadata"]["name"]:
            raise RuntimeError("A running source bundle differs from the reviewed baseline.")
        patch = {"spec": {"template": {"spec": {"volumes": [{"name": "cloud-code", "configMap": {
            "name": code["metadata"]["name"], "items": items}}]}}}}
        rollback = {"spec": {"template": {"spec": {"volumes": [copy.deepcopy(prior)]}}}}
        if name == "traffic-training-worker":
            patch = neural_patch(tree, neural, code, items)
            main = next(value for value in neural["spec"]["template"]["spec"]["containers"] if value["name"] == "training-worker")
            old_env = {value["name"]: value for value in main.get("env", [])}
            rollback["spec"]["template"]["spec"].update(
                volumes=[copy.deepcopy(prior), *[{"name": key, "$patch": "delete"} for key in ("shap-runtime", "shap-dependencies")]],
                initContainers=[{"name": "install-shap", "$patch": "delete"}],
                containers=[{"name": "training-worker", "volumeMounts": [{"mountPath": path, "$patch": "delete"}
                    for path in ("/opt/traffic-shap", "/opt/traffic-shap-deps")],
                    "env": [old_env.get(key, {"name": key, "$patch": "delete"}) for key in ("NUMBA_NUM_THREADS", "NUMBA_CACHE_DIR")]}])
        documents[name + ".patch.json"], documents[name + ".rollback.json"] = patch, rollback
    specifications = {}
    for name in ROLES:
        before = cluster.call("get", "deployment", name, "-o", "json", json_result=True)
        proposed = cluster.call("patch", "deployment", name, "--type=strategic", "--dry-run=server", "-o", "json",
                               "--patch-file=-", content=json.dumps(documents[name + ".patch.json"]), json_result=True)
        if before["metadata"]["generation"] != baseline["deployments"][name]["generation"]:
            raise RuntimeError("Deployment changed during preparation.")
        specifications[name] = {"before": hashlib.sha256(canonical(before["spec"])).hexdigest(),
                                "after": hashlib.sha256(canonical(proposed["spec"])).hexdigest()}
    documents["deployment-spec-hashes.json"] = specifications
    directory.mkdir(parents=True)
    review = {"bundle_id": manifest["bundle_id"], "baseline_bundle_id": old["bundle_id"], "changed_files": sorted(changed),
              "migration_required": False, "airflow_changed": False, "cloud_flags_changed": False, "files": {},
              "operator_sha256": digest(__file__), "preparer_sha256": digest(ROOT / "scripts/cloud_training/prepare_rollout.py")}
    for name, value in documents.items():
        (directory / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        review["files"][name] = digest(directory / name)
    (directory / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"prepared": True, "bundle_id": manifest["bundle_id"], "cluster_changed": False}


class NeuralRollout:
    def __init__(self, kubectl, directory):
        self.directory = Path(directory)
        self.review = read(self.directory / "review.json")
        if self.review["operator_sha256"] != digest(__file__) or self.review["preparer_sha256"] != digest(ROOT / "scripts/cloud_training/prepare_rollout.py"):
            raise RuntimeError("A rollout helper changed after prepare.")
        for name, expected in self.review["files"].items():
            if Path(name).name != name or digest(self.directory / name) != expected:
                raise RuntimeError("A prepared review file changed.")
        resource = next(item for item in read(self.directory / "configmaps.json")["items"]
                        if item["metadata"]["name"].startswith("traffic-cloud-") and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
        if code_manifest(resource) != code_map(sources())[2]:
            raise RuntimeError("Source changed after prepare; create a fresh package.")
        self.cluster = Rollout(kubectl, PRIOR, PRIOR / "baseline.json")
        self.baseline = read(self.directory / "baseline.json")
        self.journal_path = self.directory / "neural-rollout-journal.json"
        self.journal = read(self.journal_path) if self.journal_path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]:
            raise RuntimeError("Journal belongs to a different bundle.")

    def save(self, **values):
        self.journal.update(values, updated_at=datetime.now(timezone.utc).isoformat())
        temporary = self.journal_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.journal, indent=2), encoding="utf-8"); temporary.replace(self.journal_path)

    def idle(self):
        ready = self.cluster.remote("traffic-api", READINESS)
        if ready["active_airflow_runs"] or ready["active_controlled_runs"] or ready["migration_heads"] != ["20261028_01"]:
            raise RuntimeError("Training/cleanup is active or the database head differs.")
        for name in ROLES[:2]:
            if self.cluster.remote(name, 'import json;from urllib.request import urlopen;print(json.dumps(json.load(urlopen("http://127.0.0.1:8000/health/integration",timeout=5))["busy"]))'):
                raise RuntimeError("A worker is busy.")
        return ready

    def guard(self, name, phases):
        current = self.cluster.call("get", "deployment", name, "-o", "json", json_result=True)
        hashes = read(self.directory / "deployment-spec-hashes.json")[name]
        if (current["metadata"]["uid"] != self.baseline["deployments"][name]["uid"]
                or hashlib.sha256(canonical(current["spec"])).hexdigest() not in {hashes[phase] for phase in phases}):
            raise RuntimeError("Deployment differs from the reviewed specifications: " + name)

    def validate(self):
        self.idle()
        for name, prior in self.baseline["deployments"].items():
            current = self.cluster.call("get", "deployment", name, "-o", "json", json_result=True)
            if current["metadata"]["uid"] != prior["uid"] or current["metadata"]["generation"] != prior["generation"]:
                raise RuntimeError("Deployment changed after preparation.")
        self.cluster.verify(expected=True, bundle_id=self.review["baseline_bundle_id"])
        self.cluster.call("apply", "--server-side", "--field-manager=traffic-cloud-training", "--dry-run=server",
                          "-f", str(self.directory / "configmaps.json"), "-o", "name")
        for name in ROLES:
            self.cluster.call("patch", "deployment", name, "--type=strategic", "--dry-run=server", "-o", "name",
                "--patch-file=-", content=(self.directory / (name + ".patch.json")).read_text(encoding="utf-8"))
        return {"server_validated_not_applied": True, "bundle_id": self.review["bundle_id"], "training_started": False}

    def check(self):
        result = self.validate()
        for name in ROLES:
            proof = read(self.directory / (name + ".source-proof.json"))
            if proof.get("source_startup_verified") is not True or proof.get("bundle_id") != self.review["bundle_id"]:
                raise RuntimeError("The exact package needs startup proofs for every role.")
            if name == "traffic-training-worker" and not (proof.get("neural_compatibility") or {}).get("passed"):
                raise RuntimeError("Neural startup needs a real compatibility proof with the reviewed dependencies.")
        if not (self.directory / "cloud-neural-proof.json").is_file():
            raise RuntimeError("The isolated Kaggle GPU SHAP check has not been completed; do not Apply yet.")
        cloud = read(self.directory / "cloud-neural-proof.json")
        if not cloud.get("passed") or not cloud.get("private_staging_cleaned") or cloud.get("fit_called") is not False:
            raise RuntimeError("The exact neural source needs the isolated GPU SHAP proof first.")
        required = {"src/shap_reporting.py", "src/models/recurrent.py", "scripts/cloud_training/execute_branch.py"}
        if (set(cloud.get("source_sha256", {})) != required or not cloud.get("remote_stopped")
                or cloud.get("runtime", {}).get("gpu_count", 0) < 1
                or {item.get("model") for item in cloud.get("results", []) if item.get("status") == "completed"
                    and item.get("model_unchanged")} != {"LSTM", "GRU"}):
            raise RuntimeError("Cloud proof must cover both neural models and the exact ML sources.")
        for name, expected in cloud["source_sha256"].items():
            if digest(ROOT / name) != expected:
                raise RuntimeError("Cloud SHAP proof belongs to older source.")
        result["neural_shap_ready_to_apply"] = True
        return result

    def apply(self):
        if self.journal.get("apply_started"):
            raise RuntimeError("Apply was already started. Inspect the journal; do not replay it.")
        self.check(); ready = self.idle()
        self.save(apply_started=True, dag_was_paused=ready["dag_paused"])
        self.cluster.pause(True); self.idle()
        self.cluster.call("apply", "--server-side", "--field-manager=traffic-cloud-training", "-f", str(self.directory / "configmaps.json"))
        for name in ROLES:
            self.guard(name, ["before"])
            self.cluster.patch(name, read(self.directory / (name + ".patch.json"))); self.save(last_patched=name)
        result = self.verify(); self.save(applied=True)
        self.cluster.pause(self.journal["dag_was_paused"])
        return {**result, "neural_shap_applied": True}

    def verify(self):
        for name in ROLES:
            self.guard(name, ["after"])
        result = self.cluster.verify(expected=True, bundle_id=self.review["bundle_id"])
        for name in ROLES:
            path = "/api/v1/health/cloud-training" if name == "traffic-api" else "/health/cloud-training"
            health = self.cluster.remote(name, 'import json;from urllib.request import urlopen;print(json.dumps(json.load(urlopen("http://127.0.0.1:8000' + path + '",timeout=5))))')
            if health.get("neural_shap_supported") is not True:
                raise RuntimeError("Neural SHAP capability was not reported: " + name)
        return {**result, "neural_shap_supported": True}

    def rollback(self):
        if not self.journal.get("apply_started"):
            raise RuntimeError("Apply has not started.")
        self.idle(); self.cluster.pause(True)
        for name in reversed(ROLES):
            self.guard(name, ["before", "after"])
            self.cluster.patch(name, read(self.directory / (name + ".rollback.json")))
        result = self.cluster.verify(expected=True, bundle_id=self.review["baseline_bundle_id"])
        self.save(rolled_back=True); self.cluster.pause(self.journal["dag_was_paused"])
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["Prepare", "Validate", "Check", "Apply", "Verify", "Rollback"])
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.kubectl, args.directory) if args.action == "Prepare" else getattr(
            NeuralRollout(args.kubectl, args.directory), args.action.lower())()
        print(json.dumps(result)); return 0
    except Exception as error:
        print(json.dumps({"neural_shap_stopped": True, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect the reviewed proof; raw details omitted."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

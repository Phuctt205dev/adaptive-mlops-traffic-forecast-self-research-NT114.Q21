"""Upgrade per-algorithm SHAP reports and Drift usage; operator Apply only."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.shap import rollout_neural_shap as base
from scripts.shap.recover_feature_importance_cache import inventory
from scripts.cloud_training.prepare_rollout import canonical, code_map, sources
from scripts.cloud_training.manage_rollout import Rollout
from scripts.cloud_training.snapshot_cluster import capture

PRIOR = ROOT / "results/feature-importance/history-v2"
DEFAULT = ROOT / "results/feature-importance/five-model-v1"
ALLOWED = {"project/" + path for path in (
    "src/pipeline.py", "src/time_series_cross_validation.py", "src/region_training_variants.py",
    "scripts/cloud_training/execute_branch.py", "backend/app/api/routes/internal_training.py",
    "backend/app/services/feature_importance.py", "backend/app/schemas/feature_importance.py",
    "backend/app/services/model_registry.py", "backend/app/services/training_tracking.py",
    "backend/app/services/drift_monitoring.py")}


def prepare(kubectl, directory):
    directory = Path(directory)
    if directory.exists():
        raise RuntimeError("Keep the existing review; use a fresh directory.")
    recovery = base.read(ROOT / "results/feature-importance/cache-recovery-v1/recovery-journal.json")
    if not recovery.get("applied") or not recovery.get("dag_state_restored"):
        raise RuntimeError("Complete the cache recovery before preparing this upgrade.")
    resources = base.read(PRIOR / "configmaps.json")["items"]
    old_code = next(item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-")
                    and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    old = base.code_manifest(old_code)
    code, items, manifest = code_map(sources())
    changed = {name for name in set(old["files"]) | set(manifest["files"])
               if old["files"].get(name) != manifest["files"].get(name)}
    if changed != ALLOWED:
        raise RuntimeError("Per-model report scope differs: " + str(sorted(changed ^ ALLOWED)))
    cluster = Rollout(kubectl, base.PRIOR, base.PRIOR / "baseline.json")
    cluster.verify(expected=True, bundle_id=old["bundle_id"])
    baseline = capture(kubectl)
    cache = next(v for v in baseline["deployments"]["traffic-api"]["volumes"] if v["name"] == "artifact-cache")
    if cache.get("emptyDir", {}).get("sizeLimit") != "4Gi":
        raise RuntimeError("This review requires the recovered 4Gi cache baseline.")
    capacity = inventory(type("CapacityCheck", (), {"cluster": cluster})())
    documents = {"baseline.json": baseline, "capacity-at-prepare.json": capacity,
        "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": [code,
            *[item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-airflow-")]]},
        "shap-dependency-config.json": base.read(PRIOR / "shap-dependency-config.json")}
    specs = {}
    for name in base.ROLES:
        volume = next(item for item in baseline["deployments"][name]["volumes"] if item["name"] == "cloud-code")
        if volume["configMap"]["name"] != old_code["metadata"]["name"]:
            raise RuntimeError("A running source bundle differs from the reviewed baseline.")
        replacement = copy.deepcopy(volume)
        replacement["configMap"].update(name=code["metadata"]["name"], items=items)
        patch = {"spec": {"template": {"spec": {"volumes": [replacement]}}}}
        documents[name + ".patch.json"] = patch
        documents[name + ".rollback.json"] = {"spec": {"template": {"spec": {"volumes": [copy.deepcopy(volume)]}}}}
        before = cluster.call("get", "deployment", name, "-o", "json", json_result=True)
        after = cluster.call("patch", "deployment", name, "--type=strategic", "--dry-run=server", "-o", "json",
                             "--patch-file=-", content=json.dumps(patch), json_result=True)
        if before["metadata"]["generation"] != baseline["deployments"][name]["generation"]:
            raise RuntimeError("Deployment changed during prepare.")
        specs[name] = {"before": hashlib.sha256(canonical(before["spec"])).hexdigest(),
                       "after": hashlib.sha256(canonical(after["spec"])).hexdigest()}
    documents["deployment-spec-hashes.json"] = specs
    directory.mkdir(parents=True)
    review = {"bundle_id": manifest["bundle_id"], "baseline_bundle_id": old["bundle_id"], "changed_files": sorted(changed),
              "operator_sha256": base.digest(base.__file__), "files": {},
              "preparer_sha256": base.digest(ROOT / "scripts/cloud_training/prepare_rollout.py"),
              "five_model_helper_sha256": base.digest(__file__),
              "capacity_helper_sha256": base.digest(ROOT / "scripts/shap/recover_feature_importance_cache.py")}
    for name, value in documents.items():
        (directory / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        review["files"][name] = base.digest(directory / name)
    (directory / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"prepared": True, "bundle_id": manifest["bundle_id"], "applied": False, "cache_capacity_checked": True}


class FiveModelRollout(base.NeuralRollout):
    def __init__(self, kubectl, directory):
        super().__init__(kubectl, directory)
        if (set(self.review.get("changed_files", [])) != ALLOWED
                or self.review.get("five_model_helper_sha256") != base.digest(__file__)
                or self.review.get("capacity_helper_sha256") != base.digest(ROOT / "scripts/shap/recover_feature_importance_cache.py")):
            raise RuntimeError("Report scope/helpers changed after review.")
        self.journal_path = self.directory / "five-model-rollout-journal.json"
        self.journal = base.read(self.journal_path) if self.journal_path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]:
            raise RuntimeError("Wrong five-model journal.")

    def check(self):
        result = super().validate()
        capacity = inventory(self)
        for name in base.ROLES:
            proof = base.read(self.directory / (name + ".source-proof.json"))
            if (proof.get("source_startup_verified") is not True or proof.get("bundle_id") != self.review["bundle_id"]
                    or proof.get("fit_called") is not False or proof.get("lifespan_not_started") is not True):
                raise RuntimeError("The exact source needs three passing entrypoint proofs.")
        return {**result, "five_model_reports_ready_to_apply": True, "startup_restore_bytes": capacity["restore_bytes"]}

    def verify(self):
        result = super().verify()
        result.pop("production_ready", None)  # Legacy helper hardcoded this unrelated acceptance flag.
        source = '''import json
from urllib.request import urlopen
s=json.load(urlopen('http://127.0.0.1:8000/openapi.json',timeout=10))
p=s['components']['schemas']['FeatureImportanceReport']['properties']
assert {'model_reports','model_average'} <= set(p)
print(json.dumps({'five_model_schema_ready':True}))
'''
        self.cluster.remote("traffic-api", source)
        return {**result, "five_model_schema_ready": True}

    def apply(self):
        result = super().apply()
        result.pop("neural_shap_applied", None)
        return {**result, "five_model_reports_applied": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["Prepare", "Validate", "Check", "Apply", "Verify", "Rollback"])
    parser.add_argument("--kubectl", default="C:/Users/ADMIN/AppData/Local/Programs/kubectl/kubectl.exe")
    parser.add_argument("--directory", default=str(DEFAULT))
    args = parser.parse_args()
    try:
        result = prepare(args.kubectl, args.directory) if args.action == "Prepare" else getattr(
            FiveModelRollout(args.kubectl, args.directory), args.action.lower())()
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"five_model_rollout_stopped": True, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect the proof; raw details omitted."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

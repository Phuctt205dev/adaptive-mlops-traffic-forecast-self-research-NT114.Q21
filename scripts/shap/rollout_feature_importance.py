"""Prepare/check history UI backend upgrade; operator owns Apply/Verify/Rollback."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.shap import rollout_neural_shap as base
from scripts.cloud_training.prepare_rollout import canonical, code_map, sources
from scripts.cloud_training.manage_rollout import Rollout
from scripts.cloud_training.snapshot_cluster import capture

PRIOR = ROOT / "results/shap-retrain/neural-metrics-v1"
DEFAULT = ROOT / "results/feature-importance/history-v2"
ALLOWED = {"project/backend/app/" + name for name in ("services/feature_importance.py", "schemas/feature_importance.py")}


def prepare(kubectl, directory):
    directory = Path(directory)
    if directory.exists():
        raise RuntimeError("Keep the old review unchanged; use a fresh directory.")
    resources = base.read(PRIOR / "configmaps.json")["items"]
    old_code = next(item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-")
                    and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    old = base.code_manifest(old_code)
    code, items, manifest = code_map(sources())
    changed = {name for name in set(old["files"]) | set(manifest["files"])
               if old["files"].get(name) != manifest["files"].get(name)}
    if changed != ALLOWED:
        raise RuntimeError("Expected only the feature importance service/schema changes: " + str(sorted(changed ^ ALLOWED)))
    cluster = Rollout(kubectl, base.PRIOR, base.PRIOR / "baseline.json")
    cluster.verify(expected=True, bundle_id=old["bundle_id"])
    baseline = capture(kubectl)
    documents = {"baseline.json": baseline, "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": [code,
        *[item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-airflow-")]]}}
    tree = cluster.call("get", "deployment", "traffic-training-tree", "-o", "json", json_result=True)
    volume = next(value for value in tree["spec"]["template"]["spec"]["volumes"] if value["name"] == "shap-runtime")
    dependencies = cluster.call("get", "configmap", volume["configMap"]["name"], "-o", "json", json_result=True)
    documents["shap-dependency-config.json"] = {"data": dependencies["data"], "name": volume["configMap"]["name"]}
    specs = {}
    for name in base.ROLES:
        volume = next(item for item in baseline["deployments"][name]["volumes"] if item["name"] == "cloud-code")
        if volume["configMap"]["name"] != old_code["metadata"]["name"]:
            raise RuntimeError("The running source baseline changed.")
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
              "operator_sha256": base.digest(base.__file__),
              "preparer_sha256": base.digest(ROOT / "scripts/cloud_training/prepare_rollout.py"),
              "history_operator_sha256": base.digest(__file__), "files": {}}
    for name, value in documents.items():
        (directory / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        review["files"][name] = base.digest(directory / name)
    (directory / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"prepared": True, "bundle_id": manifest["bundle_id"], "applied": False}


class FeatureImportanceRollout(base.NeuralRollout):
    def __init__(self, kubectl, directory):
        super().__init__(kubectl, directory)
        if (set(self.review.get("changed_files", [])) != ALLOWED
                or self.review.get("history_operator_sha256") != base.digest(__file__)):
            raise RuntimeError("History scope/helper changed after review.")
        self.journal_path = self.directory / "history-rollout-journal.json"
        self.journal = base.read(self.journal_path) if self.journal_path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]:
            raise RuntimeError("Wrong history rollout journal.")

    def check(self):
        result = super().validate()
        for name in base.ROLES:
            proof = base.read(self.directory / (name + ".source-proof.json"))
            if (proof.get("source_startup_verified") is not True or proof.get("bundle_id") != self.review["bundle_id"]
                    or proof.get("fit_called") is not False or proof.get("lifespan_not_started") is not True):
                raise RuntimeError("The exact bundle needs a passing entrypoint proof for each role.")
        return {**result, "feature_importance_history_ready_to_apply": True}

    def apply(self):
        result = super().apply()
        result.pop("neural_shap_applied", None)
        return {**result, "feature_importance_history_applied": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["Prepare", "Validate", "Check", "Apply", "Verify", "Rollback"])
    parser.add_argument("--kubectl", default="C:/Users/ADMIN/AppData/Local/Programs/kubectl/kubectl.exe")
    parser.add_argument("--directory", default=str(DEFAULT))
    args = parser.parse_args()
    try:
        result = prepare(args.kubectl, args.directory) if args.action == "Prepare" else getattr(
            FeatureImportanceRollout(args.kubectl, args.directory), args.action.lower())()
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"feature_importance_stopped": True, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect the review; raw details omitted."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

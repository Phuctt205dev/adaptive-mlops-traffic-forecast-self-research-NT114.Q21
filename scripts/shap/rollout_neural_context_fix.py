"""Neural retrain context + safe diagnostics. Operator applies; Prepare/Check read only."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.shap import rollout_neural_shap as base
from scripts.shap import rollout_retrain_selection as retrain
from scripts.cloud_training.prepare_rollout import canonical, code_map, sources
from scripts.cloud_training.manage_rollout import Rollout
from scripts.cloud_training.snapshot_cluster import capture

PRIOR = ROOT / "results/shap-retrain/dtype-fix-v1"
DEFAULT = ROOT / "results/shap-retrain/neural-context-v1"
ALLOWED = {"project/" + name for name in (
    "src/time_series_cross_validation.py", "scripts/cloud_training/execute_branch.py",
    "src/cloud_training/errors.py", "src/cloud_training/notebook.py", "src/cloud_training/kaggle_rpc.py",
    "src/cloud_training/kaggle_execution.py", "backend/app/services/training_control.py")}


def prepare(kubectl, directory):
    directory = Path(directory)
    if directory.exists():
        raise RuntimeError("Preserve existing proofs; choose a new output directory.")
    resources = base.read(PRIOR / "configmaps.json")["items"]
    old_code = next(item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-")
                    and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    old = base.code_manifest(old_code)
    code, items, manifest = code_map(sources())
    changed = {name for name in set(old["files"]) | set(manifest["files"])
               if old["files"].get(name) != manifest["files"].get(name)}
    if changed != ALLOWED:
        raise RuntimeError("Neural context/diagnostic source scope differs: " + str(sorted(changed ^ ALLOWED)))
    cluster = Rollout(kubectl, base.PRIOR, base.PRIOR / "baseline.json")
    cluster.verify(expected=True, bundle_id=old["bundle_id"])
    baseline = capture(kubectl)
    documents = {"baseline.json": baseline, "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": [code,
        *[item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-airflow-")]]}}
    specs = {}
    for name in base.ROLES:
        volume = next(item for item in baseline["deployments"][name]["volumes"] if item["name"] == "cloud-code")
        if volume["configMap"]["name"] != old_code["metadata"]["name"]:
            raise RuntimeError("The live source baseline changed.")
        replacement = copy.deepcopy(volume)
        replacement["configMap"].update(name=code["metadata"]["name"], items=items)
        patch = {"spec": {"template": {"spec": {"volumes": [replacement]}}}}
        documents[name + ".patch.json"] = patch
        documents[name + ".rollback.json"] = {"spec": {"template": {"spec": {"volumes": [copy.deepcopy(volume)]}}}}
        before = cluster.call("get", "deployment", name, "-o", "json", json_result=True)
        after = cluster.call("patch", "deployment", name, "--type=strategic", "--dry-run=server", "-o", "json",
                             "--patch-file=-", content=json.dumps(patch), json_result=True)
        if before["metadata"]["generation"] != baseline["deployments"][name]["generation"]:
            raise RuntimeError("A deployment changed during preparation.")
        specs[name] = {"before": hashlib.sha256(canonical(before["spec"])).hexdigest(),
                       "after": hashlib.sha256(canonical(after["spec"])).hexdigest()}
    documents["deployment-spec-hashes.json"] = specs
    directory.mkdir(parents=True)
    review = {"bundle_id": manifest["bundle_id"], "baseline_bundle_id": old["bundle_id"], "changed_files": sorted(changed),
              "operator_sha256": base.digest(base.__file__),
              "preparer_sha256": base.digest(ROOT / "scripts/cloud_training/prepare_rollout.py"),
              "context_operator_sha256": base.digest(__file__), "retrain_helper_sha256": base.digest(retrain.__file__), "files": {}}
    for name, value in documents.items():
        (directory / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        review["files"][name] = base.digest(directory / name)
    (directory / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"prepared": True, "bundle_id": manifest["bundle_id"], "applied": False}


class ContextFix(retrain.RetrainRollout):
    def __init__(self, kubectl, directory):
        base.NeuralRollout.__init__(self, kubectl, directory)
        if (set(self.review.get("changed_files", [])) != ALLOWED
                or self.review.get("context_operator_sha256") != base.digest(__file__)
                or self.review.get("retrain_helper_sha256") != base.digest(retrain.__file__)):
            raise RuntimeError("Context fix scope/helper changed.")
        self.journal_path = self.directory / "context-rollout-journal.json"
        self.journal = base.read(self.journal_path) if self.journal_path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]:
            raise RuntimeError("Wrong context rollout journal.")

    def check(self):
        result = super().check()
        cloud = base.read(self.directory / "cloud-neural-fold-proof.json")
        if (not cloud.get("passed") or not cloud.get("remote_stopped") or not cloud.get("private_staging_cleaned")
                or cloud.get("fit_called") is not True or cloud.get("synthetic_only") is not True):
            raise RuntimeError("Tiny synthetic real-fit GPU proof and cleanup are required.")
        required = {"src/time_series_cross_validation.py", "src/feature_selection.py", "src/models/recurrent.py"}
        if set(cloud.get("source_sha256", {})) != required:
            raise RuntimeError("Cloud proof source scope differs.")
        for name, expected in cloud["source_sha256"].items():
            if base.digest(ROOT / name) != expected:
                raise RuntimeError("The GPU fold proof belongs to older ML source.")
        if {item.get("model") for item in cloud.get("results", []) if item.get("passed")
                and item.get("history_context_preserved") and item.get("training_targets_in_window")} != {"LSTM", "GRU"}:
            raise RuntimeError("Both real neural folds must preserve history and target boundaries.")
        return {**result, "neural_context_fix_ready_to_apply": True}

    def apply(self):
        return {**super().apply(), "neural_context_fix_applied": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["Prepare", "Validate", "Check", "Apply", "Verify", "Rollback"])
    parser.add_argument("--kubectl", default="C:/Users/ADMIN/AppData/Local/Programs/kubectl/kubectl.exe")
    parser.add_argument("--directory", default=str(DEFAULT))
    args = parser.parse_args()
    try:
        result = prepare(args.kubectl, args.directory) if args.action == "Prepare" else getattr(
            ContextFix(args.kubectl, args.directory), args.action.lower())()
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"context_fix_stopped": True, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect proof; raw details omitted."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

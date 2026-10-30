"""Single-module SHAP dtype correction; explicit user Apply, no training."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.shap import rollout_retrain_selection as retrain
from scripts.shap import rollout_neural_shap as base
from scripts.cloud_training.prepare_rollout import canonical, code_map, sources
from scripts.cloud_training.manage_rollout import Rollout
from scripts.cloud_training.snapshot_cluster import capture

PRIOR = ROOT / "results/shap-retrain/review-v3"
DEFAULT = ROOT / "results/shap-retrain/dtype-fix-v1"
MODULE = "project/src/feature_selection.py"


def prepare(kubectl, directory):
    directory = Path(directory)
    if directory.exists():
        raise RuntimeError("Keep old evidence; use a fresh output directory.")
    resources = base.read(PRIOR / "configmaps.json")["items"]
    old_code = next(item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-")
                    and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    old = base.code_manifest(old_code)
    code, items, manifest = code_map(sources())
    changed = {name for name in set(old["files"]) | set(manifest["files"])
               if old["files"].get(name) != manifest["files"].get(name)}
    if changed != {MODULE}:
        raise RuntimeError("Only the reviewed feature_selection dtype module may change.")
    cluster = Rollout(kubectl, base.PRIOR, base.PRIOR / "baseline.json")
    cluster.verify(expected=True, bundle_id=old["bundle_id"])
    baseline = capture(kubectl)
    documents = {"baseline.json": baseline, "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": [code,
        *[item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-airflow-")]]}}
    specifications = {}
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
        specifications[name] = {"before": hashlib.sha256(canonical(before["spec"])).hexdigest(),
                                "after": hashlib.sha256(canonical(after["spec"])).hexdigest()}
    documents["deployment-spec-hashes.json"] = specifications
    directory.mkdir(parents=True)
    review = {"bundle_id": manifest["bundle_id"], "baseline_bundle_id": old["bundle_id"], "changed_files": [MODULE],
              "operator_sha256": base.digest(base.__file__),
              "preparer_sha256": base.digest(ROOT / "scripts/cloud_training/prepare_rollout.py"),
              "dtype_operator_sha256": base.digest(__file__), "retrain_helper_sha256": base.digest(retrain.__file__), "files": {}}
    for name, value in documents.items():
        (directory / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        review["files"][name] = base.digest(directory / name)
    (directory / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"prepared": True, "bundle_id": manifest["bundle_id"], "applied": False}


class DtypeFix(retrain.RetrainRollout):
    def __init__(self, kubectl, directory):
        base.NeuralRollout.__init__(self, kubectl, directory)
        if (self.review.get("changed_files") != [MODULE]
                or self.review.get("dtype_operator_sha256") != base.digest(__file__)
                or self.review.get("retrain_helper_sha256") != base.digest(retrain.__file__)):
            raise RuntimeError("Dtype fix scope/helper changed after preparation.")
        self.journal_path = self.directory / "dtype-rollout-journal.json"
        self.journal = base.read(self.journal_path) if self.journal_path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]:
            raise RuntimeError("Wrong dtype rollout journal.")

    def apply(self):
        result = super().apply()
        return {**result, "retrain_dtype_fix_applied": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["Prepare", "Validate", "Check", "Apply", "Verify", "Rollback"])
    parser.add_argument("--kubectl", default="C:/Users/ADMIN/AppData/Local/Programs/kubectl/kubectl.exe")
    parser.add_argument("--directory", default=str(DEFAULT))
    args = parser.parse_args()
    try:
        result = prepare(args.kubectl, args.directory) if args.action == "Prepare" else getattr(
            DtypeFix(args.kubectl, args.directory), args.action.lower())()
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"dtype_fix_stopped": True, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect proof; raw details omitted."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

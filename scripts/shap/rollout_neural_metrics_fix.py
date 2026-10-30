"""Preserve neural CV metrics and harden supervisor reads/diagnostics; operator Apply."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.shap import rollout_neural_shap as base
from scripts.shap import rollout_neural_context_fix as context
from scripts.cloud_training.prepare_rollout import canonical, code_map, sources
from scripts.cloud_training.manage_rollout import Rollout
from scripts.cloud_training.snapshot_cluster import capture

PRIOR = ROOT / "results/shap-retrain/neural-context-v1"
DEFAULT = ROOT / "results/shap-retrain/neural-metrics-v1"
ALLOWED = {"project/" + name for name in ("src/time_series_cross_validation.py", "src/cloud_training/notebook.py",
    "src/cloud_training/kaggle_rpc.py", "src/cloud_training/kaggle_execution.py")}


def prepare(kubectl, directory):
    directory = Path(directory)
    if directory.exists():
        raise RuntimeError("Keep the previous review; use a fresh output directory.")
    resources = base.read(PRIOR / "configmaps.json")["items"]
    old_code = next(item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-")
                    and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    old = base.code_manifest(old_code)
    code, items, manifest = code_map(sources())
    changed = {name for name in set(old["files"]) | set(manifest["files"])
               if old["files"].get(name) != manifest["files"].get(name)}
    if changed != ALLOWED:
        raise RuntimeError("Metric/supervisor scope differs: " + str(sorted(changed ^ ALLOWED)))
    cluster = Rollout(kubectl, base.PRIOR, base.PRIOR / "baseline.json")
    cluster.verify(expected=True, bundle_id=old["bundle_id"])
    baseline = capture(kubectl)
    documents = {"baseline.json": baseline, "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": [code,
        *[item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-airflow-")]]}}
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
              "metrics_operator_sha256": base.digest(__file__), "context_helper_sha256": base.digest(context.__file__),
              "retrain_helper_sha256": base.digest(context.retrain.__file__), "files": {}}
    for name, value in documents.items():
        (directory / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        review["files"][name] = base.digest(directory / name)
    (directory / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"prepared": True, "bundle_id": manifest["bundle_id"], "applied": False}


class MetricsFix(context.ContextFix):
    def __init__(self, kubectl, directory):
        base.NeuralRollout.__init__(self, kubectl, directory)
        if (set(self.review.get("changed_files", [])) != ALLOWED
                or self.review.get("metrics_operator_sha256") != base.digest(__file__)
                or self.review.get("context_helper_sha256") != base.digest(context.__file__)
                or self.review.get("retrain_helper_sha256") != base.digest(context.retrain.__file__)):
            raise RuntimeError("Metric fix scope/helpers changed.")
        self.journal_path = self.directory / "metrics-rollout-journal.json"
        self.journal = base.read(self.journal_path) if self.journal_path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]:
            raise RuntimeError("Wrong metric rollout journal.")

    def check(self):
        result = super().check()
        cloud = base.read(self.directory / "cloud-neural-fold-proof.json")
        if {row.get("model") for row in cloud.get("results", []) if row.get("full_pipeline_completed")
                and row.get("selection_mode") == "reduced" and row.get("final_report_ready") and row.get("reload_predict")
                and set(row.get("cv_metric_names", [])) == {"MAE", "RMSE", "MAPE", "WAPE", "R2"}} != {"LSTM", "GRU"}:
            raise RuntimeError("Both models need complete GPU CV/final-fit/report/reload proof with all metrics.")
        return {**result, "neural_metrics_fix_ready_to_apply": True}

    def apply(self):
        return {**super().apply(), "neural_metrics_fix_applied": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["Prepare", "Validate", "Check", "Apply", "Verify", "Rollback"])
    parser.add_argument("--kubectl", default="C:/Users/ADMIN/AppData/Local/Programs/kubectl/kubectl.exe")
    parser.add_argument("--directory", default=str(DEFAULT))
    args = parser.parse_args()
    try:
        result = prepare(args.kubectl, args.directory) if args.action == "Prepare" else getattr(
            MetricsFix(args.kubectl, args.directory), args.action.lower())()
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"metrics_fix_stopped": True, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect proof; raw details omitted."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Immutable source update. Prepare/Check read only; operator runs Apply/Verify/Rollback."""
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

PRIOR = ROOT / "results/model-delete-fix/review-v1"
DEFAULT = ROOT / "results/shap-retrain/review-v3"
ALLOWED = {"project/" + name for name in (
    "src/feature_selection.py", "src/pipeline.py", "src/lstm_sequences.py", "src/time_series_cross_validation.py",
    "src/region_training_variants.py", "scripts/cloud_training/execute_branch.py",
    "backend/app/core/config.py", "backend/app/schemas/model_version.py", "backend/app/services/model_registry.py",
    "backend/app/services/drift_monitoring.py", "backend/app/services/retrain_selection.py",
    "backend/app/api/routes/datasets.py", "backend/app/api/routes/internal_training.py",
    "backend/app/api/routes/drift.py", "backend/app/api/routes/internal_drift.py")}


def prepare(kubectl, directory):
    directory = Path(directory)
    if directory.exists():
        raise RuntimeError("Keep existing evidence; choose a fresh review directory.")
    prior_resources = base.read(PRIOR / "configmaps.json")["items"]
    old_code = next(item for item in prior_resources if item["metadata"]["name"].startswith("traffic-cloud-")
                    and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    old = base.code_manifest(old_code)
    code, items, manifest = code_map(sources())
    changed = {name for name in set(old["files"]) | set(manifest["files"])
               if old["files"].get(name) != manifest["files"].get(name)}
    if changed != ALLOWED:
        raise RuntimeError("Retrain source scope differs: " + str(sorted(changed ^ ALLOWED)))
    cluster = Rollout(kubectl, base.PRIOR, base.PRIOR / "baseline.json")
    cluster.verify(expected=True, bundle_id=old["bundle_id"])
    baseline = capture(kubectl)
    documents = {"baseline.json": baseline, "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": [code,
        *[item for item in prior_resources if item["metadata"]["name"].startswith("traffic-cloud-airflow-")]]}}
    specifications = {}
    for name in base.ROLES:
        volume = next(item for item in baseline["deployments"][name]["volumes"] if item["name"] == "cloud-code")
        if volume["configMap"]["name"] != old_code["metadata"]["name"]:
            raise RuntimeError("A live source bundle differs; review the new baseline.")
        replacement = copy.deepcopy(volume)
        replacement["configMap"].update(name=code["metadata"]["name"], items=items)
        patch = {"spec": {"template": {"spec": {"volumes": [replacement]}}}}
        documents[name + ".patch.json"] = patch
        documents[name + ".rollback.json"] = {"spec": {"template": {"spec": {"volumes": [copy.deepcopy(volume)]}}}}
        before = cluster.call("get", "deployment", name, "-o", "json", json_result=True)
        proposed = cluster.call("patch", "deployment", name, "--type=strategic", "--dry-run=server", "-o", "json",
                                "--patch-file=-", content=json.dumps(patch), json_result=True)
        if before["metadata"]["generation"] != baseline["deployments"][name]["generation"]:
            raise RuntimeError("A deployment changed during prepare.")
        specifications[name] = {"before": hashlib.sha256(canonical(before["spec"])).hexdigest(),
                                "after": hashlib.sha256(canonical(proposed["spec"])).hexdigest()}
    documents["deployment-spec-hashes.json"] = specifications
    directory.mkdir(parents=True)
    review = {"bundle_id": manifest["bundle_id"], "baseline_bundle_id": old["bundle_id"],
              "changed_files": sorted(changed), "operator_sha256": base.digest(base.__file__),
              "preparer_sha256": base.digest(ROOT / "scripts/cloud_training/prepare_rollout.py"),
              "retrain_operator_sha256": base.digest(__file__), "files": {}}
    for name, value in documents.items():
        (directory / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        review["files"][name] = base.digest(directory / name)
    (directory / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"prepared": True, "bundle_id": manifest["bundle_id"], "applied": False}


class RetrainRollout(base.NeuralRollout):
    def __init__(self, kubectl, directory):
        super().__init__(kubectl, directory)
        if set(self.review.get("changed_files", [])) != ALLOWED or self.review.get("retrain_operator_sha256") != base.digest(__file__):
            raise RuntimeError("Retrain scope/operator changed.")
        self.journal_path = self.directory / "retrain-rollout-journal.json"
        self.journal = base.read(self.journal_path) if self.journal_path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]:
            raise RuntimeError("Wrong retrain rollout journal.")

    def check(self):
        result = self.validate()
        for name in base.ROLES:
            proof = base.read(self.directory / (name + ".source-proof.json"))
            if proof.get("source_startup_verified") is not True or proof.get("bundle_id") != self.review["bundle_id"]:
                raise RuntimeError("All three roles require fresh source proofs.")
        compatibility = base.read(self.directory / "selection-compatibility.json")
        if not compatibility.get("passed") or compatibility.get("bundle_id") != self.review["bundle_id"] or compatibility.get("fit_called") is not False:
            raise RuntimeError("The reviewed neural feature mask needs a real no-fit compatibility proof.")
        if (compatibility.get("lifespan_started") is not False
                or {item.get("model") for item in compatibility.get("results", [])
                    if all(item.get(key) is True for key in ("mask_roundtrip", "shap_ranking", "reload_predict"))} != {"LSTM", "GRU"}):
            raise RuntimeError("Both neural models must pass mask, SHAP and save/reload compatibility.")
        return {**result, "retrain_selection_ready_to_apply": True}

    def apply(self):
        result = super().apply()
        result.pop("neural_shap_applied", None)
        return {**result, "retrain_selection_applied": True}

    def rollback(self):
        # The old sequence transform ignores the new channel mask. Refuse a
        # source rollback while any registered model could require that mask.
        result = self.cluster.remote("traffic-api", '''import json
from sqlalchemy import select
from backend.app.db.models import TrainingRun,ModelVersion
from backend.app.db.session import SessionLocal
with SessionLocal() as db:
 rows=db.execute(select(TrainingRun.configuration_json).join(ModelVersion,ModelVersion.training_run_id==TrainingRun.id)).scalars()
 count=sum(1 for config in rows if (config or {}).get("model_family")=="neural_sequence" and (config or {}).get("feature_selection_result",{}).get("mode")=="reduced")
 print(json.dumps({"reduced_neural_models":count}))
''')
        if result["reduced_neural_models"]:
            raise RuntimeError("Registered reduced-channel neural models require the new serving code; source rollback is blocked.")
        return super().rollback()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["Prepare", "Validate", "Check", "Apply", "Verify", "Rollback"])
    parser.add_argument("--kubectl", default="C:/Users/ADMIN/AppData/Local/Programs/kubectl/kubectl.exe")
    parser.add_argument("--directory", default=str(DEFAULT))
    args = parser.parse_args()
    try:
        result = prepare(args.kubectl, args.directory) if args.action == "Prepare" else getattr(
            RetrainRollout(args.kubectl, args.directory), args.action.lower())()
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"retrain_rollout_stopped": True, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect proof; raw details omitted."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

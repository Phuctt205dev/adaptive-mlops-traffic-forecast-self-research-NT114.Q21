"""Operator-owned output transfer and truthful Airflow skipped-state patch."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

try:
    from .manage_rollout import DEPLOYMENTS, READINESS, Rollout
    from .prepare_rollout import canonical, code_map, sources
    from .snapshot_cluster import capture
except ImportError:
    from manage_rollout import DEPLOYMENTS, READINESS, Rollout
    from prepare_rollout import canonical, code_map, sources
    from snapshot_cluster import capture

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "results/cloud-training/cloud-result-fix"
ALLOWED = {"project/src/cloud_training/kaggle_execution.py", "project/src/cloud_training/kaggle_transport.py"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def prepare(kubectl, *, directory=None, previous_review=None, allowed=None):
    directory = Path(directory) if directory else DIRECTORY
    previous_review = Path(previous_review) if previous_review else ROOT / "results/cloud-training/cloud-network-fix"
    allowed = allowed if allowed is not None else ALLOWED
    if directory.exists():
        raise RuntimeError("Keep the existing prepared result fix and journal unchanged.")
    previous = next(item for item in read(previous_review / "configmaps.json")["items"]
                    if item["metadata"]["name"].startswith("traffic-cloud-") and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    old = next(json.loads(value) for value in previous["data"].values() if value.startswith('{"bundle_id":'))
    code, items, manifest = code_map(sources())
    changed = {name for name in set(old["files"]) | set(manifest["files"]) if old["files"].get(name) != manifest["files"].get(name)}
    if changed != allowed:
        raise RuntimeError("Application changes differ from the reviewed two-file output fix.")
    data = {"dag.py": (ROOT / "Deployment_test_on_rke2/runtime/traffic_region_training_cloud.py").read_text(encoding="utf-8"),
            "legacy.py": (ROOT / "Deployment_test_on_rke2/runtime/traffic_region_training_worker.py").read_text(encoding="utf-8"),
            "airflow_control.py": (ROOT / "src/cloud_training/airflow_control.py").read_text(encoding="utf-8")}
    airflow = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"namespace": "traffic-mlops",
               "name": "traffic-cloud-airflow-" + hashlib.sha256(canonical(data)).hexdigest()[:12]}, "immutable": True, "data": data}
    baseline = capture(kubectl)
    documents = {"baseline.json": baseline, "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": [code, airflow]}}
    for name in DEPLOYMENTS:
        volume_name = "cloud-airflow" if name.startswith("airflow") else "cloud-code"
        prior = next(value for value in baseline["deployments"][name]["volumes"] if value["name"] == volume_name)
        replacement = copy.deepcopy(prior)
        replacement["configMap"]["name"] = airflow["metadata"]["name"] if name.startswith("airflow") else code["metadata"]["name"]
        if not name.startswith("airflow"):
            replacement["configMap"]["items"] = items
        documents[name + ".patch.json"] = {"spec": {"template": {"spec": {"volumes": [replacement]}}}}
        documents[name + ".rollback.json"] = {"spec": {"template": {"spec": {"volumes": [copy.deepcopy(prior)]}}}}
    review = {"bundle_id": manifest["bundle_id"], "baseline_bundle_id": old["bundle_id"], "changed_files": sorted(changed),
              "airflow_skipped_states_changed": True, "migration_required": False, "cloud_flags_changed": False, "files": {}}
    directory.mkdir(parents=True)
    for name, value in documents.items():
        raw = json.dumps(value, indent=2).encode();(directory / name).write_bytes(raw)
        review["files"][name] = hashlib.sha256(raw).hexdigest()
    (directory / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"prepared": True, "bundle_id": review["bundle_id"], "cluster_changed": False}


class ResultFix:
    def __init__(self, kubectl, *, directory=None, baseline_review=None):
        self.directory = Path(directory) if directory else DIRECTORY
        prior = Path(baseline_review) if baseline_review else ROOT / "results/cloud-training/review-final"
        self.review = read(self.directory / "review.json")
        self.baseline = read(self.directory / "baseline.json")
        self.after = Rollout(kubectl, self.directory, self.directory / "baseline.json")
        self.before = Rollout(kubectl, prior, (prior / "baseline.json") if (prior / "baseline.json").exists() else ROOT / "results/cloud-training/snapshot-20261028-v2.json")
        self.path = self.directory / "result-fix-journal.json"
        self.journal = read(self.path) if self.path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]:
            raise RuntimeError("Result-fix journal belongs to a different bundle.")

    def save(self, **values):
        self.journal.update(values, updated_at=datetime.now(timezone.utc).isoformat())
        temporary = self.path.with_suffix(".tmp");temporary.write_text(json.dumps(self.journal, indent=2), encoding="utf-8");temporary.replace(self.path)

    def idle(self):
        result = self.before.remote("traffic-api", READINESS)
        if result["active_airflow_runs"] or result["active_controlled_runs"] or result["migration_heads"] != ["20261028_01"]:
            raise RuntimeError("Training is still active/awaiting stop or the migration differs.")
        for name in DEPLOYMENTS[:2]:
            if self.before.remote(name, 'import json;from urllib.request import urlopen;print(json.dumps(json.load(urlopen("http://127.0.0.1:8000/health/integration",timeout=5))["busy"]))'):
                raise RuntimeError("A training worker is busy.")
        return result

    def check(self):
        for name in DEPLOYMENTS:
            live = self.before.call("get", "deployment", name, "-o", "json", json_result=True)
            baseline = self.baseline["deployments"][name]
            if live["metadata"]["uid"] != baseline["uid"] or live["metadata"]["generation"] != baseline["generation"]:
                raise RuntimeError("A deployment changed after result-fix preparation.")
        for name in DEPLOYMENTS[:3]:
            proof = read(self.directory / (name + ".source-proof.json"))
            if proof.get("source_startup_verified") is not True or proof.get("bundle_id") != self.review["bundle_id"]:
                raise RuntimeError("The exact result fix needs all three runtime startup proofs.")
        airflow = read(self.directory / "airflow-source-proof.json")
        skipped = read(self.directory / "airflow-skip-proof.json")
        for proof in (airflow, skipped):
            if proof.get("configmaps_sha256") != self.review["files"]["configmaps.json"]:
                raise RuntimeError("Airflow proofs differ from the reviewed sources.")
        if not airflow.get("skipped_receipts_verified") or not skipped.get("real_airflow_skip_xcom_verified"):
            raise RuntimeError("Real skipped-state/XCom/finalize checks have not passed.")
        self.additional_proofs()
        ready = self.idle()
        self.before.verify(expected=True, bundle_id=self.review["baseline_bundle_id"])
        self.after.call("apply", "--server-side", "--field-manager=traffic-cloud-training", "--dry-run=server", "-f", str(self.directory / "configmaps.json"), "-o", "name")
        for name in DEPLOYMENTS:
            self.after.call("patch", "deployment", name, "--type=strategic", "--dry-run=server", "-o", "name", "--patch-file=-",
                            content=(self.directory / (name + ".patch.json")).read_text(encoding="utf-8"))
        return {"result_fix_check_passed": True, "bundle_id": self.review["bundle_id"], "dag_paused": ready["dag_paused"], "cluster_changed": False}

    def additional_proofs(self):
        transfer = read(ROOT / "results/cloud-training/artifact-worker-candidate-final.json")
        if (transfer.get("write_pending") or not transfer.get("result", {}).get("proof_passed")
                or transfer.get("candidate_source_sha256") != hashlib.sha256((ROOT / "src/cloud_training/kaggle_execution.py").read_bytes()).hexdigest()):
            raise RuntimeError("The exact output receiver needs a passing Kaggle-to-worker RF/CPU-reload/cleanup proof.")

    def apply(self):
        if self.journal.get("apply_started"):
            raise RuntimeError("The result fix was already started. Inspect the journal before changing anything again.")
        ready = self.check();self.save(apply_started=True, dag_was_paused=ready["dag_paused"])
        self.before.pause(True);self.idle()
        self.after.call("apply", "--server-side", "--field-manager=traffic-cloud-training", "-f", str(self.directory / "configmaps.json"))
        for name in DEPLOYMENTS:
            self.after.patch(name, read(self.directory / (name + ".patch.json")));self.save(last_patched=name)
        self.verify();self.save(applied=True);self.after.pause(self.journal["dag_was_paused"])
        return {"result_fix_applied": True, "cloud_enabled": True, "bundle_id": self.review["bundle_id"], "training_started": False}

    def verify(self):
        result = self.after.verify(expected=True)
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        (self.directory / "verify-proof.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    def rollback(self):
        if not self.journal.get("apply_started"):
            raise RuntimeError("The result fix has not been started.")
        self.idle();self.before.pause(True)
        for name in reversed(DEPLOYMENTS):
            self.after.patch(name, read(self.directory / (name + ".rollback.json")))
        self.before.verify(expected=True, bundle_id=self.review["baseline_bundle_id"])
        self.save(rolled_back=True);self.before.pause(self.journal["dag_was_paused"])
        return {"result_fix_rolled_back": True, "database_unchanged": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=["Prepare", "Check", "Apply", "Verify", "Rollback"], default="Check")
    parser.add_argument("--kubectl", required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.kubectl) if args.action == "Prepare" else getattr(ResultFix(args.kubectl), args.action.lower())()
        if args.action == "Check":
            result["checked_at"] = datetime.now(timezone.utc).isoformat()
            (DIRECTORY / "check-proof.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({"result_fix_failed": True, "action": args.action, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect the sanitized proof/journal; raw details omitted."}))
        raise SystemExit(2)

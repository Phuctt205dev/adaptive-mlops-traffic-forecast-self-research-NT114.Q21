"""Narrow operator-owned cloud RPC fix. Default Check is read-only/dry-run."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

try:
    from .manage_rollout import READINESS, Rollout
    from .prepare_rollout import code_map, sources
    from .snapshot_cluster import capture
except ImportError:
    from manage_rollout import READINESS, Rollout
    from prepare_rollout import code_map, sources
    from snapshot_cluster import capture

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "results/cloud-training/cloud-network-fix"
ROLES = ("traffic-training-tree", "traffic-training-worker", "traffic-api")
ALLOWED = {"project/src/cloud_training/kaggle_rpc.py", "project/src/cloud_training/kaggle_transport.py",
           "project/backend/app/services/training_control.py"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def prepare(kubectl):
    if DIRECTORY.exists():
        raise RuntimeError("Keep the existing prepared fix unchanged; do not overwrite its proofs/journal.")
    original = ROOT / "results/cloud-training/review-final"
    resources = read(original / "configmaps.json")["items"]
    old = next(item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-") and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    manifest = next(json.loads(value) for value in old["data"].values() if value.startswith('{"bundle_id":'))
    current, items, new = code_map(sources())
    changed = {name for name in set(manifest["files"]) | set(new["files"]) if manifest["files"].get(name) != new["files"].get(name)}
    if changed != ALLOWED:
        raise RuntimeError("The proposed changes exceed/differ from the reviewed three-file RPC fix.")
    baseline = capture(kubectl)
    DIRECTORY.mkdir(parents=True)
    documents = {"baseline.json": baseline, "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": [current]}}
    for name in ROLES:
        volume = next(item for item in baseline["deployments"][name]["volumes"] if item["name"] == "cloud-code")
        documents[name + ".patch.json"] = {"spec": {"template": {"spec": {"volumes": [
            {"name": "cloud-code", "configMap": {"name": current["metadata"]["name"], "items": items}}]}}}}
        documents[name + ".rollback.json"] = {"spec": {"template": {"spec": {"volumes": [copy.deepcopy(volume)]}}}}
    review = {"bundle_id": new["bundle_id"], "baseline_bundle_id": manifest["bundle_id"],
              "changed_files": sorted(changed), "cloud_flags_changed": False, "migration_required": False,
              "airflow_changed": False, "files": {}}
    for name, value in documents.items():
        raw = json.dumps(value, indent=2).encode()
        (DIRECTORY / name).write_bytes(raw)
        review["files"][name] = hashlib.sha256(raw).hexdigest()
    (DIRECTORY / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"prepared": True, "bundle_id": new["bundle_id"], "changed_files": sorted(changed), "cluster_changed": False}


class Hotfix:
    def __init__(self, kubectl):
        self.review = read(DIRECTORY / "review.json")
        for name, digest in self.review["files"].items():
            path = DIRECTORY / name
            if path.parent != DIRECTORY or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError("The prepared fix differs from its review hashes.")
        self.baseline = read(DIRECTORY / "baseline.json")
        self.cluster = Rollout(kubectl, ROOT / "results/cloud-training/review-final", ROOT / "results/cloud-training/snapshot-20261028-v2.json")
        self.path = DIRECTORY / "hotfix-journal.json"
        self.journal = read(self.path) if self.path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]:
            raise RuntimeError("The fix journal belongs to another bundle.")

    def save(self, **values):
        self.journal.update(values, updated_at=datetime.now(timezone.utc).isoformat())
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.journal, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def idle(self):
        result = self.cluster.remote("traffic-api", READINESS)
        if result["active_airflow_runs"] or result["active_controlled_runs"] or result["migration_heads"] != ["20261028_01"]:
            raise RuntimeError("Training is active/awaiting cleanup or the DB head differs. Wait before changing workers.")
        for name in ROLES[:2]:
            busy = self.cluster.remote(name, 'import json;from urllib.request import urlopen;print(json.dumps(json.load(urlopen("http://127.0.0.1:8000/health/integration",timeout=5))["busy"]))')
            if busy:
                raise RuntimeError("A training worker is still busy.")
        return result

    def check(self):
        for name in ROLES:
            current = self.cluster.call("get", "deployment", name, "-o", "json", json_result=True)
            baseline = self.baseline["deployments"][name]
            if current["metadata"]["uid"] != baseline["uid"] or current["metadata"]["generation"] != baseline["generation"]:
                raise RuntimeError("A deployment changed after fix preparation. Inspect before proceeding.")
            proof = read(DIRECTORY / (name + ".source-proof.json"))
            if proof.get("source_startup_verified") is not True or proof.get("bundle_id") != self.review["bundle_id"]:
                raise RuntimeError("The exact fix needs a passing source startup check on all three roles.")
        for name in ("tree", "neural"):
            proof = read(ROOT / ("results/cloud-training/connection-upload-" + name + "-final.json"))
            expected = hashlib.sha256((ROOT / "src/cloud_training/kaggle_rpc.py").read_bytes()).hexdigest()
            result = proof.get("result", {})
            if (proof["rpc_sha256"] != expected or proof.get("write_pending") or result.get("probe_passed") is not True
                    or result.get("private_version_verified") is not True or result.get("cleaned") is not True):
                raise RuntimeError("The exact RPC fix needs passing private upload/cleanup checks from both workers.")
        ready = self.idle()
        self.cluster.verify(expected=True)
        self.cluster.call("apply", "--server-side", "--field-manager=traffic-cloud-training", "--dry-run=server", "-f", str(DIRECTORY / "configmaps.json"), "-o", "name")
        for name in ROLES:
            self.cluster.call("patch", "deployment", name, "--type=strategic", "--dry-run=server", "-o", "name", "--patch-file=-",
                              content=(DIRECTORY / (name + ".patch.json")).read_text(encoding="utf-8"))
        return {"hotfix_check_passed": True, "bundle_id": self.review["bundle_id"], "dag_paused": ready["dag_paused"], "cluster_changed": False}

    def apply(self):
        if self.journal.get("apply_started"):
            raise RuntimeError("This fix was already started. Inspect its journal; do not replay uncertain changes.")
        ready = self.check()
        self.save(apply_started=True, dag_was_paused=ready["dag_paused"])
        self.cluster.pause(True)
        self.idle()
        self.cluster.call("apply", "--server-side", "--field-manager=traffic-cloud-training", "-f", str(DIRECTORY / "configmaps.json"))
        for name in ROLES:
            self.cluster.patch(name, read(DIRECTORY / (name + ".patch.json")))
            self.save(last_patched=name)
        self.verify()
        self.save(applied=True)
        self.cluster.pause(self.journal["dag_was_paused"])
        return {"hotfix_applied": True, "cloud_enabled": True, "bundle_id": self.review["bundle_id"], "training_started": False}

    def verify(self):
        result = self.cluster.verify(expected=True, bundle_id=self.review["bundle_id"])
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        (DIRECTORY / "verify-proof.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    def rollback(self):
        if not self.journal.get("apply_started"):
            raise RuntimeError("This fix has not been started; there is nothing to roll back.")
        self.idle()
        self.cluster.pause(True)
        for name in reversed(ROLES):
            self.cluster.patch(name, read(DIRECTORY / (name + ".rollback.json")))
        self.cluster.verify(expected=True)
        self.save(rolled_back=True)
        self.cluster.pause(self.journal["dag_was_paused"])
        return {"hotfix_rolled_back": True, "database_unchanged": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=["Prepare", "Check", "Apply", "Verify", "Rollback"], default="Check")
    parser.add_argument("--kubectl", required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.kubectl) if args.action == "Prepare" else getattr(Hotfix(args.kubectl), args.action.lower())()
        if args.action == "Check":
            result["checked_at"] = datetime.now(timezone.utc).isoformat()
            (DIRECTORY / "check-proof.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({"hotfix_action_failed": True, "action": args.action, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect the local proof/journal; raw details omitted."}))
        raise SystemExit(2)

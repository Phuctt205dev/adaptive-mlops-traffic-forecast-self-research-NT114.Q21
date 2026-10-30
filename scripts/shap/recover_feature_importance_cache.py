"""Guarded storage recovery for the partially applied history-v2 rollout.

Prepare/Check/Verify only read the cluster. Operator Apply/Resume recover API
rollout and restore the DAG's recorded pause state. No object or model deletion.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.shap import rollout_neural_shap as base
from scripts.shap.rollout_feature_importance import FeatureImportanceRollout, DEFAULT as HISTORY
from scripts.cloud_training.prepare_rollout import canonical

DEFAULT = ROOT / "results/feature-importance/cache-recovery-v1"
GIB = 1024 ** 3
CACHE_BYTES = 4 * GIB
PREFIXES = ("models", "results", "monitoring", "mlruns", "data_versions", "data")
INVENTORY = '''import json,os
from backend.app.services.storage import get_s3_client
bucket=os.environ['TRAFFIC_API_RUNTIME_BUCKET'];counts={}
client=get_s3_client()
for prefix in ('models','results','monitoring','mlruns','data_versions','data'):
 size=0;count=0
 for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=prefix+'/'):
  for item in page.get('Contents',[]):
   if item['Key'].endswith(('/','.tmp')):continue
   size+=item['Size'];count+=1
 counts[prefix]={'bytes':size,'objects':count}
print(json.dumps({'prefixes':counts,'restore_bytes':sum(x['bytes'] for x in counts.values())}))
'''


def storage_patch(spec):
    """Resize only disk accounting; retain memory, image, commands and sources."""
    pod = spec["template"]["spec"]
    volume = next(v for v in pod["volumes"] if v["name"] == "artifact-cache")
    if volume.get("emptyDir", {}).get("sizeLimit") != "2Gi":
        raise RuntimeError("The reviewed 2Gi artifact cache baseline changed.")
    api = next(c for c in pod["containers"] if c["name"] == "traffic-api")
    restore = next(c for c in pod["initContainers"] if c["name"] == "restore-artifact-cache")
    resources = copy.deepcopy(api["resources"])
    resources["limits"]["ephemeral-storage"] = "4Gi"
    resources["requests"]["ephemeral-storage"] = "3Gi"
    init_resources = copy.deepcopy(restore["resources"])
    init_resources["limits"]["ephemeral-storage"] = "5Gi"
    init_resources["requests"]["ephemeral-storage"] = "3Gi"
    return {"spec": {"template": {"spec": {
        "volumes": [{"name": "artifact-cache", "emptyDir": {"sizeLimit": "4Gi"}}],
        "containers": [{"name": "traffic-api", "resources": resources}],
        "initContainers": [{"name": "restore-artifact-cache", "resources": init_resources}],
    }}}}


def check_capacity(inventory, nodes):
    size = inventory["restore_bytes"]
    # Headroom also covers block rounding, new downloads and ordinary cache growth.
    if not isinstance(size, int) or size < 0 or size > CACHE_BYTES * 4 // 5:
        raise RuntimeError("Startup restore exceeds 80% of the proposed 4Gi cache. Prepare a larger storage review.")
    if set(nodes) != {"mlops-node-02", "mlops-node-03"} or any(
            value["availableBytes"] < CACHE_BYTES + 2 * GIB for value in nodes.values()):
        raise RuntimeError("A serving node has insufficient free disk for the reviewed recovery.")


def inventory(operator):
    cluster = operator.cluster
    pod = cluster.pod("traffic-api")  # Select the surviving fully ready replica.
    result = cluster.call("exec", "-i", pod, "-c", "traffic-api", "--", "python", "-B", "-",
                          content=INVENTORY, timeout=100, json_result=True)
    nodes = {}
    for name in ("mlops-node-02", "mlops-node-03"):
        summary = cluster.call("get", "--raw", "/api/v1/nodes/" + name + "/proxy/stats/summary", json_result=True)
        nodes[name] = {key: summary["node"]["fs"][key] for key in ("availableBytes", "capacityBytes")}
    check_capacity(result, nodes)
    return {**result, "nodes": nodes, "target_cache_bytes": CACHE_BYTES, "read_only": True}


def spec_digest(spec):
    return hashlib.sha256(canonical(spec)).hexdigest()


def prepare(kubectl, directory):
    directory = Path(directory)
    if directory.exists():
        raise RuntimeError("Keep the previous recovery review; use a fresh directory.")
    operator = FeatureImportanceRollout(kubectl, HISTORY)
    if not operator.journal.get("apply_started") or operator.journal.get("applied"):
        raise RuntimeError("Recovery requires the recorded partial history-v2 Apply.")
    operator.idle()
    for name in base.ROLES:
        operator.guard(name, ["after"])
    capacity = inventory(operator)
    before = operator.cluster.call("get", "deployment", "traffic-api", "-o", "json", json_result=True)
    patch = storage_patch(before["spec"])
    after = operator.cluster.call("patch", "deployment", "traffic-api", "--type=strategic", "--dry-run=server",
        "-o", "json", "--patch-file=-", content=json.dumps(patch), json_result=True)
    # Persist only review metadata and the narrow patch, never raw Deployment/env.
    directory.mkdir(parents=True)
    (directory / "api-cache.patch.json").write_text(json.dumps(patch, indent=2), encoding="utf-8")
    review = {"bundle_id": operator.review["bundle_id"], "uid": before["metadata"]["uid"],
        "before_spec_sha256": spec_digest(before["spec"]), "after_spec_sha256": spec_digest(after["spec"]),
        "dag_was_paused": operator.journal["dag_was_paused"], "capacity_at_prepare": capacity,
        "helper_sha256": base.digest(__file__), "patch_sha256": base.digest(directory / "api-cache.patch.json"),
        "history_review_sha256": base.digest(HISTORY / "review.json"),
        "history_journal_sha256": base.digest(HISTORY / "history-rollout-journal.json")}
    (directory / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"recovery_prepared": True, "restore_bytes": capacity["restore_bytes"],
            "cache_before": "2Gi", "cache_after": "4Gi", "applied": False}


class CacheRecovery:
    def __init__(self, kubectl, directory):
        self.directory = Path(directory)
        self.review = base.read(self.directory / "review.json")
        for path, expected in ((Path(__file__), self.review["helper_sha256"]),
                (self.directory / "api-cache.patch.json", self.review["patch_sha256"]),
                (HISTORY / "review.json", self.review["history_review_sha256"]),
                (HISTORY / "history-rollout-journal.json", self.review["history_journal_sha256"])):
            if base.digest(path) != expected:
                raise RuntimeError("Recovery source, patch or original review/journal changed.")
        self.original = FeatureImportanceRollout(kubectl, HISTORY)
        self.cluster = self.original.cluster
        if self.review["bundle_id"] != self.original.review["bundle_id"]:
            raise RuntimeError("Recovery bundle differs from the original reviewed source.")
        self.journal_path = self.directory / "recovery-journal.json"
        self.journal = base.read(self.journal_path) if self.journal_path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]:
            raise RuntimeError("Wrong recovery journal.")

    def guard(self, phase):
        for name in base.ROLES[:2]:
            self.original.guard(name, ["after"])
        current = self.cluster.call("get", "deployment", "traffic-api", "-o", "json", json_result=True)
        if (current["metadata"]["uid"] != self.review["uid"]
                or spec_digest(current["spec"]) != self.review[phase + "_spec_sha256"]):
            raise RuntimeError("API specification changed outside the recovery review.")

    def check(self):
        self.guard("before")
        self.original.idle()
        capacity = inventory(self.original)
        self.cluster.call("patch", "deployment", "traffic-api", "--type=strategic", "--dry-run=server", "-o", "name",
            "--patch-file=-", content=(self.directory / "api-cache.patch.json").read_text(encoding="utf-8"))
        return {"cache_recovery_ready_to_apply": True, "restore_bytes": capacity["restore_bytes"],
                "target_cache_bytes": CACHE_BYTES, "training_started": False, "cluster_changed": False}

    def save(self, **values):
        self.journal.update(values, updated_at=datetime.now(timezone.utc).isoformat())
        temporary = self.journal_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.journal, indent=2), encoding="utf-8")
        temporary.replace(self.journal_path)

    def verify(self):
        self.guard("after")
        result = self.cluster.verify(expected=True, bundle_id=self.review["bundle_id"])
        inventory(self.original)
        # Inspect the serving process' OpenAPI, not a fresh Python import of the image.
        source = '''import json
from urllib.request import urlopen
schema=json.load(urlopen('http://127.0.0.1:8000/openapi.json',timeout=10))
assert 'history' in schema['components']['schemas']['FeatureImportanceReport']['properties']
print(json.dumps({'history_api_schema_ready':True}))
'''
        self.cluster.remote("traffic-api", source)
        return {**result, "cache_recovery_verified": True, "cache_size": "4Gi", "history_api_schema_ready": True}

    def complete(self):
        result = self.verify()
        # Persist verified state before restoring scheduling; Resume can retry if restoration fails.
        self.save(rollout_verified=True)
        self.cluster.pause(self.review["dag_was_paused"])
        self.save(applied=True, dag_state_restored=True)
        return {**result, "dag_state_restored": True}

    def apply(self):
        if self.journal.get("apply_started"):
            raise RuntimeError("Recovery Apply already started. Use Verify, or Resume to finish; do not replay Apply.")
        self.check()
        self.save(apply_started=True)
        self.cluster.pause(True)
        self.original.idle()
        self.guard("before")
        self.cluster.patch("traffic-api", base.read(self.directory / "api-cache.patch.json"))
        return self.complete()

    def resume(self):
        if not self.journal.get("apply_started") or self.journal.get("applied"):
            raise RuntimeError("Resume requires a started, incomplete recovery; otherwise use Verify.")
        self.original.idle()
        return self.complete()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["Prepare", "Check", "Apply", "Verify", "Resume"])
    parser.add_argument("--kubectl", default="C:/Users/ADMIN/AppData/Local/Programs/kubectl/kubectl.exe")
    parser.add_argument("--directory", default=str(DEFAULT))
    args = parser.parse_args()
    try:
        result = prepare(args.kubectl, args.directory) if args.action == "Prepare" else getattr(
            CacheRecovery(args.kubectl, args.directory), args.action.lower())()
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"cache_recovery_stopped": True, "error_type": type(error).__name__,
                         "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect the reviewed recovery; raw details omitted."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Operator-run recovery when the guard lease expired during an automatic role rollback.

Case (run 124, 27/11/2026): the engine began its rollback and restored every patched resource,
but its guard lease (2400 s) expired while it waited for a candidate pod to stop, so the
receipt was never closed: phase=rollback, guard_held=true, recovery_required=true, no
`resources`. `roles_deploy.py Rollback` then refuses ("Lease lost") and `recover_cd.py`
refuses (receipt not closed). Both refusals are correct; this command adds the missing proof.

Default is READ-ONLY: it compares the live cluster with the original receipt AND the private
inverse journal written ahead of each patch (results/cicd/private/role-transactions/), and
prints a sanitized proof. Ownership is released only with --release, through the same
`recover_cd.release_ownership` (Auto stays blocked until a verified Manual release).
It never patches resources, edits receipts, unpauses DAGs or trains.

    python -B scripts/cicd/recover_lease_lost.py --receipt <receipt.json> --inverses <private journal.json>
        --kubectl kubectl --kubeconfig <dedicated kubeconfig> --context traffic-delivery
        --namespace-uid <uid> [--proof-output <ignored path>] [--release]   (GH_TOKEN, GITHUB_REPOSITORY for --release)
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cicd.cd_state import GitHub, Store, emit
from scripts.cicd.deploy import Cluster, ready_pods
from scripts.cicd.recover_cd import release_ownership
from scripts.cicd.release import canonical, digest
from scripts.cicd.roles_deploy import IDLE, SERVICES

COHORTS = {"api": ("api", "api-next"), "tree": ("train-tree", "tree-next"), "neural": ("train-neural", "neural-next")}
ROUTES = {"traffic-api": "api", "traffic-api-training": "api", "api-peers": "api", "api-preview": "api",
          "traffic-training-tree": "tree", "traffic-training-worker": "neural"}
ALWAYS_READY = ("airflow-scheduler", "airflow-webserver")


def load(receipt_path, inverse_path):
    receipt_path, inverse_path = Path(receipt_path), Path(inverse_path)
    if receipt_path.name != inverse_path.name:   # the engine names both after the same transaction
        raise ValueError("Receipt and inverse journal are not from the same transaction.")
    return (json.loads(receipt_path.read_text(encoding="utf-8-sig")),
            json.loads(inverse_path.read_text(encoding="utf-8-sig")))


def prove(cluster, receipt, inverses, active):
    """Read-only proof that the interrupted rollback reached the exact BEFORE state."""
    if receipt.get("commit") != active.get("commit"):
        raise ValueError("The receipt belongs to another transaction.")
    if (receipt.get("schema") != "traffic-role-delivery/v2" or receipt.get("phase") != "rollback"
            or receipt.get("completed") or receipt.get("rolled_back")
            or not receipt.get("guard_held") or not receipt.get("recovery_required")):
        raise ValueError("Only an unfinished role rollback with a lost lease is handled here; use recover_cd.py otherwise.")
    if not isinstance(inverses, list) or sorted(p.get("name") for p in inverses) != sorted(receipt.get("patched") or []):
        raise ValueError("The inverse journal does not cover exactly the patched resources.")
    if cluster.call("get", "namespace", "traffic-mlops", "-o", "json")["metadata"]["uid"] != cluster.namespace_uid:
        raise ValueError("Wrong namespace identity.")
    pods = cluster.call("get", "pods", "-o", "json")["items"]

    restored = []
    for plan in inverses:
        if plan.get("kind") not in {"deployment", "service"}:
            raise ValueError("Unexpected resource kind in the inverse journal.")
        value = cluster.call("get", plan["kind"], plan["name"], "-o", "json")
        if value["metadata"]["uid"] != plan["uid"]:
            raise ValueError("Resource ownership changed: " + plan["name"])
        if value["spec"] != plan["before"]:
            raise ValueError("Resource is not restored to its BEFORE spec: " + plan["name"])
        restored.append({"kind": plan["kind"], "name": plan["name"], "uid_matches": True, "spec_equals_before": True})

    previous = receipt.get("active_before")
    if previous not in COHORTS["api"]:
        raise ValueError("The receipt does not name the previous API cohort.")
    serving = {}
    for role, (blue, green) in COHORTS.items():
        selected = {cluster.call("get", "service", name, "-o", "json")["spec"]["selector"]["app"]
                    for name, owner in ROUTES.items() if owner == role}
        if len(selected) != 1 or next(iter(selected)) not in (blue, green):
            raise ValueError("Services of the " + role + " role do not route to one cohort.")
        live = next(iter(selected))
        if role == "api" and live != previous:
            raise ValueError("API traffic does not route to the previous cohort.")
        serving[role] = live
        candidate = green if live == blue else blue
        if cluster.deployment(candidate)["spec"].get("replicas", 0) != 0:
            raise ValueError("A candidate cohort still has replicas: " + candidate)
    ready = 0
    for name in [*serving.values(), *ALWAYS_READY]:
        value = cluster.deployment(name)
        if not value["spec"].get("replicas") or len(ready_pods(value, pods)) != value["spec"]["replicas"]:
            raise ValueError("A serving deployment is not fully Ready: " + name)
        ready += 1

    job = cluster.call("get", "job", receipt["guard_job"], "--ignore-not-found", "-o", "json") if receipt.get("guard_job") else None
    if job and not any(c.get("type") in {"Complete", "Failed"} and c.get("status") == "True"
                       for c in job.get("status", {}).get("conditions", [])):
        raise ValueError("The original guard job is still active.")
    guards = [p for p in pods if p["metadata"]["name"] == receipt.get("guard_pod")
              or p["metadata"].get("labels", {}).get("job-name") == receipt.get("guard_job")]
    if any(p.get("status", {}).get("phase") not in {"Succeeded", "Failed"} for p in guards):
        raise ValueError("The original guard pod has not terminated.")

    idle = cluster.remote(cluster.deployment(serving["api"]), IDLE)
    if (idle["active"] or idle["running"] or idle["paused"] != receipt.get("dag_was_paused")
            or idle["drift_paused"] != receipt.get("drift_was_paused")):
        raise ValueError("Training dispatch is not idle in the recorded state.")
    return {"recovery_verified": True, "deployments_ready": ready, "case": "lease_lost_during_rollback",
            "commit": receipt["commit"], "release_id": receipt.get("release_id"),
            "receipt_sha256": digest(canonical(receipt)), "inverse_sha256": digest(canonical(inverses)),
            "restored": restored, "routing": {name: serving[owner] for name, owner in ROUTES.items()},
            "guard_terminated": True, "dispatch": {"paused": idle["paused"], "drift_paused": idle["drift_paused"],
                                                  "active": 0, "running": 0}}


def release(store, active, proof, *, expected_success):
    return release_ownership(store, active, proof, expected_success=expected_success)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("receipt", "inverses", "kubectl", "kubeconfig", "context", "namespace-uid"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--proof-output")
    parser.add_argument("--release", action="store_true", help="write the verified recovery to the CD state")
    args = parser.parse_args()
    store = Store(GitHub(os.environ["GITHUB_REPOSITORY"]))
    state = store.load()[0]
    active = state["active"]
    if active is None:
        raise ValueError("No stranded ownership exists; no recovery change is needed.")
    receipt, inverses = load(args.receipt, args.inverses)
    cluster = Cluster(args.kubectl, args.context, args.namespace_uid, args.kubeconfig)
    proof = prove(cluster, receipt, inverses, active)
    if args.proof_output:
        Path(args.proof_output).write_text(json.dumps(proof, indent=2), encoding="utf-8")
    if not args.release:
        emit({"read_only_proof": proof, "released": False})
        return
    emit(release(store, active, proof, expected_success=state["last_success"]))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"recovery_confirm_failed": True, "error_type": type(error).__name__,
                          "reason": str(error) if isinstance(error, ValueError) else "Details omitted."}))
        raise SystemExit(1)

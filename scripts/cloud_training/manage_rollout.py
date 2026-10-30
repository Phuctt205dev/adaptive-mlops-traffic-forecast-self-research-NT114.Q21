"""User-run rollout. Preflight is read-only; every mutation requires an explicit action."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

DEPLOYMENTS = ("traffic-training-tree", "traffic-training-worker", "traffic-api", "airflow-webserver", "airflow-scheduler")
READINESS = '''import json
from sqlalchemy import text,inspect
from backend.app.db.session import SessionLocal
from backend.app.core.config import get_settings
import requests
s=get_settings();out={"active_airflow_runs":0,"active_controlled_runs":0}
with SessionLocal() as db:
 out["migration_heads"]=list(db.execute(text("SELECT version_num FROM alembic_version")).scalars())
 if inspect(db.connection()).has_table("training_executions"):
  out["active_controlled_runs"]=db.execute(text("SELECT count(*) FROM training_executions WHERE state IN ('queued','running','finalizing','cancelling')")).scalar()
with requests.Session() as client:
 client.trust_env=False
 base=s.airflow_api_url+"/dags/"+s.training_dag_id
 result=client.get(base,auth=(s.airflow_admin_username,s.airflow_admin_password),timeout=5);result.raise_for_status()
 out["dag_paused"]=result.json()["is_paused"]
 for state in ['running','queued']:
  result=client.get(base+'/dagRuns',params={"state":state,"limit":100},auth=(s.airflow_admin_username,s.airflow_admin_password),timeout=5);result.raise_for_status()
  out["active_airflow_runs"]+=result.json()["total_entries"]
print(json.dumps(out))
'''


class Rollout:
    def __init__(self, kubectl, review, snapshot):
        self.kubectl, self.root = kubectl, Path(review)
        self.review = json.loads((self.root / "review.json").read_text())
        self.snapshot = json.loads(Path(snapshot).read_text(encoding="utf-8"))
        for name, digest in self.review["files"].items():
            path = self.root / name
            if path.parent != self.root or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError("Review files changed after preparation.")
        self.journal_path = self.root / "rollout-journal.json"
        self.journal = json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]:
            raise RuntimeError("Rollout journal belongs to another bundle.")

    def save(self, **values):
        self.journal.update(values, updated_at=datetime.now(timezone.utc).isoformat())
        temporary = self.journal_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.journal, indent=2), encoding="utf-8")
        temporary.replace(self.journal_path)

    def call(self, *args, content=None, timeout=50, json_result=False):
        # kubectl patch reads --patch-file as a literal filename; '-' is not
        # portable stdin support. Keep JSON out of shell quoting and process args.
        with tempfile.TemporaryDirectory(prefix="traffic-cloud-kubectl-") as directory:
            arguments = list(args)
            if "--patch-file=-" in arguments:
                path = Path(directory) / "patch.json"
                path.write_text(content, encoding="utf-8")
                arguments[arguments.index("--patch-file=-")] = "--patch-file=" + str(path)
                content = None
            request_timeout = "90s" if args[0] == "exec" and timeout >= 100 else "25s"
            result = subprocess.run([self.kubectl, "--request-timeout=" + request_timeout, "-n", "traffic-mlops", *arguments],
                input=content, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            raise RuntimeError("Cluster operation failed (" + args[0] + "); raw credential-bearing output omitted.")
        return json.loads(result.stdout) if json_result else result.stdout

    def pod(self, name):
        pods = self.call("get", "pods", "-o", "json", json_result=True)["items"]
        values = [p for p in pods if p["metadata"]["name"].startswith(name + "-") and p["status"]["phase"] == "Running"
                  and not p["metadata"].get("deletionTimestamp") and all(x.get("ready") for x in p["status"].get("containerStatuses", []))]
        if not values:
            raise RuntimeError("A required pod is not ready.")
        return values[0]["metadata"]["name"]

    def remote(self, name, source):
        return self.remote_pod(name, self.pod(name), source)

    def remote_pod(self, name, pod, source):
        if name.startswith("airflow"):
            container = name
        else:
            container = self.snapshot["runtime"][name]["main_container"]
        return self.call("exec", pod, "-c", container, "--", "python", "-B", "-c", source, timeout=100, json_result=True)

    def preflight(self, *, baseline=True):
        for name in DEPLOYMENTS:
            current = self.call("get", "deployment", name, "-o", "json", json_result=True)
            prior = self.snapshot["deployments"][name]
            if current["metadata"]["uid"] != prior["uid"]:
                raise RuntimeError("A deployment was recreated after the snapshot. Prepare a new review.")
            if baseline and current["metadata"]["generation"] != prior["generation"]:
                raise RuntimeError("A deployment changed after the snapshot. Prepare a new review.")
        for name in DEPLOYMENTS[:3]:
            proof = json.loads((self.root / (name + ".source-proof.json")).read_text())
            if proof.get("source_startup_verified") is not True or proof.get("bundle_id") != self.review["bundle_id"]:
                raise RuntimeError("This exact bundle needs a passing source check on all three runtime roles.")
        sdk = json.loads((self.root / "sdk-linux-proof.json").read_text())
        if sdk.get("isolated_linux_sdk_verified") is not True:
            raise RuntimeError("The isolated Linux SDK check has not passed.")
        airflow = json.loads((self.root / "airflow-source-proof.json").read_text(encoding="utf-8"))
        if airflow.get("airflow_source_verified") is not True or airflow.get("configmaps_sha256") != self.review["files"]["configmaps.json"]:
            raise RuntimeError("The reviewed DAG needs a passing import/graph check in Airflow.")
        if baseline:
            source = 'import hashlib,json;from pathlib import Path;print(json.dumps({"sha256":hashlib.sha256(Path("/opt/airflow/dags/traffic_region_training.py").read_bytes()).hexdigest()}))'
            for name in DEPLOYMENTS[3:]:
                if self.remote(name, source)["sha256"] != self.review["legacy_dag_sha256"]:
                    raise RuntimeError("The running legacy DAG differs from the reviewed source.")
        readiness = self.remote("traffic-api", READINESS)
        if readiness["active_airflow_runs"] or readiness["active_controlled_runs"]:
            raise RuntimeError("Training work is still active. Wait for it to stop before rollout/rollback.")
        if readiness["migration_heads"] not in [["20260625_01"], ["20261028_01"]]:
            raise RuntimeError("Database migration head differs from the review.")
        for name in DEPLOYMENTS[:2]:
            health = self.remote(name, 'import json;from urllib.request import urlopen;print(json.dumps(json.load(urlopen("http://127.0.0.1:8000/health/integration",timeout=5))["busy"]))')
            if health:
                raise RuntimeError("A training worker is still busy.")
        readiness["credential_secret_present"] = bool(self.call("get", "secret", "traffic-kaggle-credentials", "--ignore-not-found", "-o", "name").strip())
        if baseline:
            self.server_dry_run()
            readiness["server_dry_run_verified"] = True
        return readiness

    def credentials(self):
        if self.call("get", "secret", "traffic-kaggle-credentials", "--ignore-not-found", "-o", "name").strip():
            return {"credential_secret_present": True, "created": False, "existing_secret_preserved": True}
        path = Path.home() / ".kaggle" / "access_token"
        if not path.is_file() or not 0 < path.stat().st_size <= 16384:
            raise RuntimeError("The saved Kaggle token file is missing or invalid. Run the existing Login helper locally.")
        self.call("create", "secret", "generic", "traffic-kaggle-credentials", "--from-file=access_token=" + str(path))
        return {"credential_secret_present": True, "created": True, "token_displayed": False}

    def verify(self, *, expected=None, bundle_id=None):
        if expected is None:
            expected = bool(self.journal.get("enabled"))
        expected_bundle = bundle_id or self.review["bundle_id"]
        resources = json.loads((self.root / "configmaps.json").read_text(encoding="utf-8"))["items"]
        airflow = next(item["data"] for item in resources if item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
        airflow_hashes = {path: hashlib.sha256(airflow[key].encode()).hexdigest() for path, key in {
            "/opt/airflow/dags/traffic_region_training.py": "dag.py",
            "/opt/traffic-cloud/airflow_control.py": "airflow_control.py",
            "/opt/traffic-cloud/legacy/traffic_region_training_worker.py": "legacy.py"}.items()}
        graph = json.loads((self.root / "airflow-source-proof.json").read_text(encoding="utf-8"))["task_graph"]
        pods = self.call("get", "pods", "-o", "json", json_result=True)["items"]
        checked = []
        for name in DEPLOYMENTS:
            deployment = self.call("get", "deployment", name, "-o", "json", json_result=True)
            desired, status = deployment["spec"].get("replicas", 1), deployment["status"]
            if (deployment["metadata"]["uid"] != self.snapshot["deployments"][name]["uid"] or desired < 1
                    or status.get("observedGeneration", 0) < deployment["metadata"]["generation"]
                    or any(status.get(key, 0) != desired for key in ("updatedReplicas", "readyReplicas", "availableReplicas"))):
                raise RuntimeError("Deployment rollout is not fully ready: " + name)
            selector = deployment["spec"]["selector"]["matchLabels"]
            # Retained Evicted/Completed pods are historical records, not replicas.
            # Keep Pending/Unknown/unready pods in the count so real failures still fail.
            selected = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")
                        and pod.get("status", {}).get("phase") not in {"Failed", "Succeeded"}
                        and all(pod["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())]
            if len(selected) != desired:
                raise RuntimeError("The ready replica count differs: " + name)
            for pod in selected:
                if pod["status"]["phase"] != "Running" or not pod["status"].get("containerStatuses") or not all(
                        item.get("ready") for item in pod["status"]["containerStatuses"]):
                    raise RuntimeError("A required pod is not ready: " + name)
                if name.startswith("airflow"):
                    source = '''import contextlib,hashlib,importlib.util,io,json
from pathlib import Path
expected=HASHES
assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest for path,digest in expected.items())
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
 spec=importlib.util.spec_from_file_location("traffic_deployed_dag_check","/opt/airflow/dags/traffic_region_training.py")
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 graph={task.task_id:sorted(task.downstream_task_ids) for task in module.dag.tasks}
assert graph==GRAPH
print(json.dumps({"dag_source_and_graph_verified":True}))
'''.replace("HASHES", repr(airflow_hashes)).replace("GRAPH", repr(graph))
                    self.remote_pod(name, pod["metadata"]["name"], source)
                else:
                    path = "/api/v1/health/cloud-training" if name == "traffic-api" else "/health/cloud-training"
                    source = f'''import json,os
from urllib.request import urlopen
with urlopen("http://127.0.0.1:8000{path}",timeout=5) as response:health=json.load(response)
health["execution_control"]=os.environ.get("TRAINING_CONTROL_ENABLED","").lower()=="true"
print(json.dumps(health))
'''
                    health = self.remote_pod(name, pod["metadata"]["name"], source)
                    if (health.get("bundle_id") != expected_bundle or health.get("cloud_enabled") is not expected
                            or health.get("execution_control") is not expected):
                        raise RuntimeError("Runtime bundle/flags differ from the expected rollout: " + name)
                checked.append({"deployment": name, "pod": pod["metadata"]["name"]})
        source = 'import json;from sqlalchemy import text;from backend.app.db.session import SessionLocal\nwith SessionLocal() as db:print(json.dumps(list(db.execute(text("SELECT version_num FROM alembic_version")).scalars())))'
        if self.remote("traffic-api", source) != ["20261028_01"]:
            raise RuntimeError("The execution table migration is not ready.")
        return {"rollout_verified": True, "cloud_enabled": expected, "bundle_id": expected_bundle,
                "checked_pods": checked, "training_started": False, "production_ready": False}

    def inspect_run(self, run_id, expected=None):
        if str(uuid.UUID(run_id)) != run_id:
            raise RuntimeError("Supply a canonical training run UUID.")
        source = '''import json
from sqlalchemy import text
from backend.app.db.session import SessionLocal
with SessionLocal() as db:
 row=db.execute(text("SELECT backend,state,cancel_requested,branches_json FROM training_executions WHERE training_run_id=:id"),{"id":RUN_ID}).mappings().one()
 count=db.execute(text("SELECT count(*) FROM model_versions WHERE training_run_id=:id"),{"id":RUN_ID}).scalar()
 active=db.execute(text("SELECT count(*) FROM regions r JOIN model_versions m ON m.id=r.active_model_version_id WHERE m.training_run_id=:id"),{"id":RUN_ID}).scalar()
 input_exists=db.execute(text("SELECT count(*) FROM training_runs t JOIN datasets d ON d.id=t.dataset_id WHERE t.id=:id"),{"id":RUN_ID}).scalar()==1
 branches={name:{"state":item["state"],"started":item.get("attempt_id") is not None,"stop_confirmed":bool(item.get("stop_confirmed")),"cleanup_confirmed":bool(item.get("cleanup_confirmed")),"staging_cleaned":bool((item.get("provider") or {}).get("staging_cleaned")),"failure":(item.get("provider") or {}).get("last_failure")} for name,item in row["branches_json"].items()}
 out={"training_run_id":RUN_ID,"backend":row["backend"],"state":row["state"],"cancel_requested":row["cancel_requested"],"registered_model_count":count,"active_model_from_this_run_count":active,"input_dataset_present":input_exists,"branches":branches,"read_only":True}
 print(json.dumps(out))
'''.replace("RUN_ID", repr(run_id))
        result = self.remote("traffic-api", source)
        if expected:
            branches = result["branches"].values()
            valid = (result["state"] == expected and result["input_dataset_present"]
                     and result["active_model_from_this_run_count"] == 0)
            if expected == "failed":
                valid = valid and result["registered_model_count"] == 0 and all(
                    item["cleanup_confirmed"] and (not item["started"] or item["stop_confirmed"]) for item in branches)
            else:
                valid = valid and result["registered_model_count"] > 0 and all(item["staging_cleaned"] for item in branches)
            result["acceptance_check_passed"] = bool(valid)
        return result

    def server_dry_run(self):
        for filename in ("configmaps.json", "migration-job.json"):
            self.call("apply", "--server-side", "--field-manager=traffic-cloud-training", "--dry-run=server",
                      "-f", str(self.root / filename), "-o", "name")
        for name in DEPLOYMENTS:
            self.call("patch", "deployment", name, "--type=strategic", "--dry-run=server", "-o", "name",
                      "--patch-file=-", content=(self.root / (name + ".patch.json")).read_text())

    def pause(self, value):
        source = '''import requests,json
from backend.app.core.config import get_settings
s=get_settings()
with requests.Session() as c:
 c.trust_env=False
 r=c.patch(s.airflow_api_url+"/dags/"+s.training_dag_id,json={"is_paused":VALUE},auth=(s.airflow_admin_username,s.airflow_admin_password),timeout=5)
 r.raise_for_status();print(json.dumps({"paused":r.json()["is_paused"]}))
'''.replace("VALUE", repr(value))
        result = self.remote("traffic-api", source)
        if result["paused"] is not value:
            raise RuntimeError("Airflow pause state could not be confirmed.")

    def patch(self, name, document):
        # UID plus fresh resourceVersion prevents blind overwrite after review.
        current = self.call("get", "deployment", name, "-o", "json", json_result=True)
        if current["metadata"]["uid"] != self.snapshot["deployments"][name]["uid"]:
            raise RuntimeError("Deployment identity changed.")
        document = {**document, "metadata": {"resourceVersion": current["metadata"]["resourceVersion"]}}
        print(json.dumps({"patching": name}), flush=True)
        self.call("patch", "deployment", name, "--type=strategic", "--patch-file=-", content=json.dumps(document))
        self.call("rollout", "status", "deployment/" + name, "--timeout=480s", timeout=500)

    def stage(self):
        if self.journal.get("stage_started"):
            raise RuntimeError("Staging was already started. Inspect its journal; do not replay uncertain changes.")
        ready = self.preflight()
        if not ready["credential_secret_present"]:
            raise RuntimeError("Create the worker-only Kaggle credential Secret from the local token file before staging.")
        self.save(stage_started=True, dag_was_paused=ready["dag_paused"])
        self.pause(True)
        self.call("apply", "--server-side", "--field-manager=traffic-cloud-training", "-f", str(self.root / "configmaps.json"))
        job = json.loads((self.root / "migration-job.json").read_text())["metadata"]["name"]
        self.call("apply", "--server-side", "--field-manager=traffic-cloud-training", "-f", str(self.root / "migration-job.json"))
        self.call("wait", "--for=condition=complete", "job/" + job, "--timeout=180s", timeout=200)
        for name in DEPLOYMENTS:
            self.patch(name, json.loads((self.root / (name + ".patch.json")).read_text()))
            self.save(last_staged_deployment=name)
        self.verify(expected=False)
        self.save(stage_complete=True)
        self.pause(self.journal["dag_was_paused"])
        return {"staged": True, "cloud_enabled": False, "bundle_id": self.review["bundle_id"]}

    def enable(self):
        if not self.journal.get("stage_complete"):
            raise RuntimeError("Complete the disabled staging rollout first.")
        self.preflight(baseline=False)
        for name in DEPLOYMENTS[:2]:
            source = '''import json,os,subprocess
request={"operation":"doctor","username":"truongphucdev"}
r=subprocess.run([os.environ["KAGGLE_PYTHON"],"-B","-m","src.cloud_training.kaggle_rpc"],input=json.dumps(request),text=True,cwd="/opt/traffic-cloud/project",capture_output=True,timeout=80)
data=json.loads(r.stdout)
print(json.dumps({"authenticated":r.returncode==0 and data.get("ok") is True and data.get("result",{}).get("authenticated") is True}))
'''
            if self.remote(name, source).get("authenticated") is not True:
                raise RuntimeError("A staged worker could not authenticate its mounted Kaggle credential.")
        self.pause(True)
        for name in DEPLOYMENTS[:3]:
            if name == "traffic-api":
                container = self.snapshot["runtime"][name]["main_container"]
            else:
                container = "training-worker"
            self.patch(name, {"spec": {"template": {"spec": {"containers": [{"name": container,
                "env": [{"name": "TRAINING_CONTROL_ENABLED", "value": "true"},
                        {"name": "CLOUD_TRAINING_ENABLED", "value": "true"}]}]}}}})
        self.verify(expected=True)
        self.save(enabled=True)
        self.pause(self.journal["dag_was_paused"])
        return {"cloud_enabled": True, "default_backend": "local", "automatic_fallback": False}

    def rollback(self):
        self.preflight(baseline=False)
        self.pause(True)
        for name in reversed(DEPLOYMENTS):
            self.patch(name, json.loads((self.root / (name + ".rollback.json")).read_text()))
        self.save(rolled_back=True, enabled=False)
        self.pause(self.journal.get("dag_was_paused", True))
        return {"rolled_back": True, "additive_table_retained": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=["Preflight", "Credentials", "Stage", "Enable", "Verify", "InspectRun", "Rollback"], default="Preflight")
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--expect", choices=["enabled", "disabled"], help="Expected flags for read-only Verify.")
    parser.add_argument("--run-id")
    parser.add_argument("--run-state", choices=["completed", "failed"])
    args = parser.parse_args()
    try:
        manager = Rollout(args.kubectl, args.review, args.snapshot)
        if args.expect and args.action != "Verify":
            raise RuntimeError("--expect applies only to Verify.")
        if (args.action == "InspectRun") != bool(args.run_id) or (args.run_state and args.action != "InspectRun"):
            raise RuntimeError("InspectRun requires --run-id; --run-state applies only to InspectRun.")
        result = (manager.inspect_run(args.run_id, args.run_state) if args.action == "InspectRun"
                  else manager.verify(expected=None if args.expect is None else args.expect == "enabled") if args.action == "Verify"
                  else manager.preflight() if args.action == "Preflight" else getattr(manager, args.action.lower())())
        if args.action == "Preflight":
            result.update(checked_at=datetime.now(timezone.utc).isoformat(), bundle_id=manager.review["bundle_id"], cluster_changed=False)
            (manager.root / "preflight-proof.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if args.action == "Verify":
            result.update(checked_at=datetime.now(timezone.utc).isoformat(), cluster_changed=False)
            (manager.root / "verify-proof.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if args.action == "InspectRun":
            result.update(checked_at=datetime.now(timezone.utc).isoformat())
            (manager.root / ("run-" + args.run_id + "-proof.json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)
        return 2 if result.get("acceptance_check_passed") is False else 0
    except Exception as error:
        print(json.dumps({"rollout_action_failed": True, "action": args.action, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Review the local journal and checks; raw details omitted."}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

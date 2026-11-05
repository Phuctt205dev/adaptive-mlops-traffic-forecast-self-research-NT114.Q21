"""Deliver a reviewed immutable release to the existing RKE2 application.

Plan/Verify are read-only (Plan includes server dry-runs). Apply runs only in
the approved delivery job. No training, activation, migration, pruning, base
manifest application or Secret reads are performed.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.cicd.release import DIGEST_IMAGE, canonical, digest, read_json, verify
from scripts.cloud_training.manage_rollout import READINESS
from scripts.shap.recover_feature_importance_cache import INVENTORY, CACHE_BYTES

ANNOTATION = "delivery.traffic/release"
COPY = "import shutil;shutil.copytree('/opt/traffic-release/{kind}','/delivery',dirs_exist_ok=True)"
GUARD_STATUS = "import json;from pathlib import Path;p=Path('/tmp/delivery-status.json');print(p.read_text() if p.exists() else json.dumps({'state':'starting'}))"
AIRFLOW_FILES = ("import hashlib,json;from pathlib import Path;"
                 "names={'dag.py':Path('/opt/airflow/dags/traffic_region_training.py'),"
                 "'airflow_control.py':Path('/opt/traffic-cloud/airflow_control.py'),"
                 "'legacy/traffic_region_training_worker.py':Path('/opt/traffic-cloud/legacy/traffic_region_training_worker.py')};"
                 "print(json.dumps({k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in names.items()}))")
DAG_CHECK = ("import importlib.util,json;"
             "s=importlib.util.spec_from_file_location('delivery_dag_check','/opt/airflow/dags/traffic_region_training.py');"
             "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);d=m.dag;"
             "expected={'validate_training_conf','train_tree_models','train_lstm','train_gru','finalize_region_training'};"
             "ok=set(d.task_ids)==expected and d.get_task('train_gru').upstream_task_ids=={'train_lstm'} "
             "and d.get_task('finalize_region_training').upstream_task_ids=={'train_tree_models','train_gru'};"
             "print(json.dumps({'dag_valid':ok}))")


def now():
    return datetime.now(timezone.utc).isoformat()


def spec_hash(deployment):
    return digest(canonical(deployment["spec"]))


def ready_pods(deployment, pods):
    labels = deployment["spec"]["selector"]["matchLabels"]
    return [p for p in pods if all(p["metadata"].get("labels", {}).get(k) == v for k, v in labels.items())
            and p.get("status", {}).get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")
            and p.get("status", {}).get("containerStatuses")
            and all(c.get("ready") for c in p["status"]["containerStatuses"])]


def container(deployment):
    entries = deployment["spec"]["template"]["spec"]["containers"]
    name = deployment["metadata"]["name"]
    matches = [(i, c) for i, c in enumerate(entries) if c["name"] == name or
               (name.startswith("traffic-") and any(p.get("containerPort") == 8000 for p in c.get("ports", [])))]
    if len(matches) != 1:
        raise RuntimeError("Main container identity is ambiguous: " + name)
    return matches[0]


def release_patch(deployment, image, release_id, *, pull_secret=None):
    """Narrow patches; inverse contains references only, never raw env/specs."""
    name = deployment["metadata"]["name"]
    pod = deployment["spec"]["template"]["spec"]
    index, main = container(deployment)
    mode = "airflow" if name.startswith("airflow-") else "cloud"
    expected = ["python", "-B", "/opt/traffic-cloud/cloud_training_runtime.py"]
    if mode == "cloud" and main.get("command") != expected:
        raise RuntimeError("Entrypoint differs from the supported runtime: " + name)
    mounts = [v for v in main.get("volumeMounts", []) if v["mountPath"] == "/opt/traffic-cloud"]
    if len(mounts) != 1:
        raise RuntimeError("Expected one runtime volume: " + name)
    volume_name = mounts[0]["name"]
    volumes = pod["volumes"]
    position = next(i for i, v in enumerate(volumes) if v["name"] == volume_name)
    original = volumes[position]
    if set(original) - {"name", "configMap", "emptyDir"} or not ({"configMap", "emptyDir"} & set(original)):
        raise RuntimeError("Unsupported runtime volume type.")
    forward, inverse = [], []
    path = f"/spec/template/spec/volumes/{position}"
    forward.append({"op": "replace", "path": path, "value": {"name": volume_name, "emptyDir": {"sizeLimit": "8Mi"}}})
    inverse.insert(0, {"op": "replace", "path": path, "value": original})
    init = {"name": "delivery-source", "image": image, "imagePullPolicy": "IfNotPresent",
            "command": ["python", "-B", "-c", COPY.format(kind=mode)],
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "250m", "memory": "128Mi"}},
            "volumeMounts": [{"name": volume_name, "mountPath": "/delivery"}]}
    old_init = pod.get("initContainers", [])
    matches = [i for i, c in enumerate(old_init) if c["name"] == "delivery-source"]
    if matches:
        if matches != [0]:
            raise RuntimeError("Delivery init container must run first.")
        path = "/spec/template/spec/initContainers/0/image"
        forward.append({"op": "replace", "path": path, "value": image})
        inverse.insert(0, {"op": "replace", "path": path, "value": old_init[0]["image"]})
    elif "initContainers" in pod:
        forward.append({"op": "add", "path": "/spec/template/spec/initContainers/0", "value": init})
        inverse.insert(0, {"op": "remove", "path": "/spec/template/spec/initContainers/0"})
    else:
        forward.append({"op": "add", "path": "/spec/template/spec/initContainers", "value": [init]})
        inverse.insert(0, {"op": "remove", "path": "/spec/template/spec/initContainers"})
    if mode == "cloud":
        path = f"/spec/template/spec/containers/{index}/image"
        forward.append({"op": "replace", "path": path, "value": image})
        inverse.insert(0, {"op": "replace", "path": path, "value": main["image"]})
    annotations = deployment["spec"]["template"]["metadata"].get("annotations")
    if annotations is None:
        forward.append({"op": "add", "path": "/spec/template/metadata/annotations", "value": {ANNOTATION: release_id}})
        inverse.insert(0, {"op": "remove", "path": "/spec/template/metadata/annotations"})
    else:
        path = "/spec/template/metadata/annotations/" + ANNOTATION.replace("/", "~1")
        forward.append({"op": "add", "path": path, "value": release_id})
        inverse.insert(0, {"op": "add", "path": path, "value": annotations[ANNOTATION]} if ANNOTATION in annotations
                       else {"op": "remove", "path": path})
    if pull_secret and not any(x["name"] == pull_secret for x in pod.get("imagePullSecrets", [])):
        if "imagePullSecrets" in pod:
            path = f"/spec/template/spec/imagePullSecrets/{len(pod['imagePullSecrets'])}"
            forward.append({"op": "add", "path": path, "value": {"name": pull_secret}})
            inverse.insert(0, {"op": "remove", "path": path})
        else:
            path = "/spec/template/spec/imagePullSecrets"
            forward.append({"op": "add", "path": path, "value": [{"name": pull_secret}]})
            inverse.insert(0, {"op": "remove", "path": path})
    return forward, inverse


class Cluster:
    def __init__(self, executable, context, namespace_uid, kubeconfig):
        if not context or not namespace_uid:
            raise ValueError("An explicit context and expected namespace UID are required.")
        if not Path(kubeconfig).is_file():
            raise ValueError("An explicit delivery kubeconfig file is required.")
        self.prefix = [executable, "--kubeconfig", str(kubeconfig), "--context", context, "--request-timeout=45s", "-n", "traffic-mlops"]
        self.namespace_uid = namespace_uid

    def call(self, *arguments, value=None, timeout=100, json_output=True):
        result = subprocess.run([*self.prefix, *arguments], input=json.dumps(value) if value is not None else None,
                                text=True, encoding="utf-8", capture_output=True, timeout=timeout)
        if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
            raise RuntimeError("Cluster operation failed: " + arguments[0] + "; raw output omitted.")
        return json.loads(result.stdout) if json_output and result.stdout.strip() else result.stdout

    def deployment(self, name):
        return self.call("get", "deployment", name, "-o", "json")

    def pod(self, deployment):
        candidates = ready_pods(deployment, self.call("get", "pods", "-o", "json")["items"])
        if not candidates:
            raise RuntimeError("A required application pod is not ready.")
        return candidates[0]["metadata"]["name"]

    def remote(self, deployment, source):
        _, main = container(deployment)
        return self.call("exec", self.pod(deployment), "-c", main["name"], "--", "python", "-B", "-c", source)

    def patch(self, deployment, operations, *, dry_run=False):
        tests = [{"op": "test", "path": "/metadata/uid", "value": deployment["metadata"]["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]}]
        # File avoids shell and Windows quoting of JSON; it holds only narrow
        # references/patches, never a raw Deployment or kubeconfig snapshot.
        with tempfile.TemporaryDirectory(prefix="traffic-delivery-") as directory:
            path = Path(directory) / "patch.json"
            path.write_text(json.dumps(tests + operations), encoding="utf-8")
            args = ["patch", "deployment", deployment["metadata"]["name"], "--type=json", "--patch-file=" + str(path), "-o", "json"]
            if dry_run:
                args.append("--dry-run=server")
            return self.call(*args)

    def wait(self, name):
        self.call("rollout", "status", "deployment/" + name, "--timeout=600s", "--request-timeout=650s", timeout=660, json_output=False)


class Delivery:
    def __init__(self, cluster, directory, image, journal, *, commit, pull_secret=None):
        if not DIGEST_IMAGE.fullmatch(image):
            raise ValueError("Use an image pinned by digest.")
        self.cluster, self.directory, self.image = cluster, Path(directory), image
        self.release = verify(directory, expected_commit=commit)
        self.path, self.pull_secret = Path(journal), pull_secret
        self.names = self.release["baseline"]["application_deployments"] + self.release["baseline"]["airflow_deployments"]
        if self.names != ["traffic-training-tree", "traffic-training-worker", "traffic-api", "airflow-scheduler", "airflow-webserver"]:
            raise ValueError("The release targets differ from the supported application scope.")
        self.state = read_json(self.path) if self.path.exists() else {"release_id": self.release["release_id"], "commit": commit, "image": image, "patched": []}
        if any(self.state[k] != v for k, v in (("release_id", self.release["release_id"]), ("commit", commit), ("image", image))):
            raise ValueError("Journal belongs to another immutable release.")

    def save(self, **values):
        self.state.update(values, updated_at=now())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def inventory(self):
        namespace = self.cluster.call("get", "namespace", "traffic-mlops", "-o", "json")
        if namespace["metadata"]["uid"] != self.cluster.namespace_uid:
            raise RuntimeError("Wrong namespace/cluster identity.")
        values = {name: self.cluster.deployment(name) for name in self.names}
        api = values["traffic-api"]
        cache = next(v for v in api["spec"]["template"]["spec"]["volumes"] if v["name"] == "artifact-cache")
        if cache.get("emptyDir", {}).get("sizeLimit") != "4Gi":
            raise RuntimeError("Unexpected API cache capacity.")
        readiness = self.cluster.remote(api, READINESS)
        if readiness["active_airflow_runs"] or readiness["active_controlled_runs"] or readiness["migration_heads"] != self.release["baseline"]["migration_heads"]:
            raise RuntimeError("Training or migration state prevents delivery.")
        capacity = self.cluster.remote(api, INVENTORY)
        if capacity["restore_bytes"] > CACHE_BYTES * 4 // 5:
            raise RuntimeError("Artifact restore exceeds the API cache budget.")
        api_pods = ready_pods(api, self.cluster.call("get", "pods", "-o", "json")["items"])
        for pod in api_pods:
            data = self.cluster.call("exec", pod["metadata"]["name"], "-c", container(api)[1]["name"], "--", "python", "-B", "-c",
                                     "import os,json;s=os.statvfs('/app/models');print(json.dumps({'available':s.f_bavail*s.f_frsize}))")
            if data["available"] < CACHE_BYTES + 2 * 1024**3:
                raise RuntimeError("A serving node lacks disk headroom.")
        for name in self.names:
            deployment = values[name]
            if not ready_pods(deployment, self.cluster.call("get", "pods", "-o", "json")["items"]):
                raise RuntimeError("A required deployment is unavailable.")
            _, main = container(deployment)
            if name.startswith("airflow-") and main["image"] != self.release["baseline"]["airflow_image"]:
                raise RuntimeError("Airflow dependency image changed.")
            if not name.startswith("airflow-") and main["image"] != self.release["baseline"]["application_base_image"]:
                if not main["image"].startswith("ghcr.io/"):
                    raise RuntimeError("Application dependency image differs from the supported baseline.")
                prefix = "/api/v1/health" if name == "traffic-api" else "/health"
                source = "import json;from urllib.request import urlopen;print(json.dumps(json.load(urlopen('http://127.0.0.1:8000" + prefix + "/release',timeout=10))))"
                prior = self.cluster.remote(deployment, source)
                if prior.get("base_image") != self.release["baseline"]["application_base_image"]:
                    raise RuntimeError("Prior release dependency contract differs.")
        return values, readiness

    def plan(self):
        values, readiness = self.inventory()
        plans = {}
        for name, deployment in values.items():
            if name.startswith("airflow-") and digest(canonical(self.cluster.remote(deployment, AIRFLOW_FILES))) == self.release["airflow_sha256"]:
                plans[name] = {"uid": deployment["metadata"]["uid"], "before": spec_hash(deployment),
                               "after": spec_hash(deployment), "patch": [], "inverse": []}
                continue
            patch, inverse = release_patch(deployment, self.image, self.release["release_id"], pull_secret=self.pull_secret)
            after = self.cluster.patch(deployment, patch, dry_run=True)
            plans[name] = {"uid": deployment["metadata"]["uid"], "before": spec_hash(deployment),
                           "after": spec_hash(after), "patch": patch, "inverse": inverse}
        return values, readiness, plans

    def health(self):
        result = []
        for name in self.names:
            deployment = self.cluster.deployment(name)
            pods = ready_pods(deployment, self.cluster.call("get", "pods", "-o", "json")["items"])
            if len(pods) != deployment["spec"].get("replicas", 1):
                raise RuntimeError("Ready replica count differs: " + name)
            _, main = container(deployment)
            for pod in pods:
                if not name.startswith("airflow-") and pod["metadata"].get("annotations", {}).get(ANNOTATION) != self.release["release_id"]:
                    raise RuntimeError("A ready pod belongs to another release.")
                if name.startswith("airflow-"):
                    actual = self.cluster.call("exec", pod["metadata"]["name"], "-c", main["name"], "--", "python", "-B", "-c", AIRFLOW_FILES)
                    if digest(canonical(actual)) != self.release["airflow_sha256"]:
                        raise RuntimeError("Airflow runtime differs from the reviewed release.")
                    dag = self.cluster.call("exec", pod["metadata"]["name"], "-c", main["name"], "--", "python", "-B", "-c", DAG_CHECK)
                    if dag.get("dag_valid") is not True:
                        raise RuntimeError("Airflow DAG import/graph is invalid.")
                else:
                    prefix = "/api/v1/health" if name == "traffic-api" else "/health"
                    script = "import json;from urllib.request import urlopen;print(json.dumps(json.load(urlopen('http://127.0.0.1:8000" + prefix + "/release',timeout=10))))"
                    actual = self.cluster.call("exec", pod["metadata"]["name"], "-c", main["name"], "--", "python", "-B", "-c", script)
                    if any(actual.get(k) != self.release[v] for k, v in (("commit", "commit"), ("release_id", "release_id"), ("bundle_id", "cloud_bundle_id"))):
                        raise RuntimeError("Running application release differs.")
                    if name == "traffic-api":
                        script = ("import json;from urllib.request import urlopen,Request;from urllib.error import HTTPError;"
                                  "ready=json.load(urlopen('http://127.0.0.1:8000/api/v1/health/ready',timeout=20));"
                                  "print(json.dumps({'ready':ready.get('status')=='ready'}))")
                        ready = self.cluster.call("exec", pod["metadata"]["name"], "-c", main["name"], "--", "python", "-B", "-c", script)
                        if ready.get("ready") is not True:
                            raise RuntimeError("API dependencies are not ready.")
                result.append({"deployment": name, "pod": pod["metadata"]["name"], "verified": True})
        return result

    def pause(self, value):
        source = ("import requests,json;from backend.app.core.config import get_settings;s=get_settings();"
                  "r=requests.patch(s.airflow_api_url+'/dags/'+s.training_dag_id,json={'is_paused':" + repr(value) + "},"
                  "auth=(s.airflow_admin_username,s.airflow_admin_password),timeout=10);r.raise_for_status();print(json.dumps({'paused':r.json()['is_paused']}))")
        if self.cluster.remote(self.cluster.deployment("traffic-api"), source).get("paused") is not value:
            raise RuntimeError("Airflow pause state was not confirmed.")

    def guard_status(self, *, release=False):
        if release:
            self.cluster.call("exec", self.state["guard_pod"], "--", "python", "-B", "-c", "from pathlib import Path;Path('/tmp/delivery-release').touch()")
        return self.cluster.call("exec", self.state["guard_pod"], "--", "python", "-B", "-c", GUARD_STATUS)

    def start_guard(self, api):
        _, main = container(api)
        env = copy.deepcopy(main.get("env", []))
        # Only variable references are transferred. Reject a literal secret
        # rather than writing it to a Job or the local delivery journal.
        env = [v for v in env if "valueFrom" in v or v["name"] in {"DATABASE_URL"}]
        if any(v["name"] == "DATABASE_URL" and "value" in v for v in env):
            raise RuntimeError("Guard requires secret/config references for DATABASE_URL.")
        name = "traffic-delivery-" + self.release["release_id"][:12] + "-" + str(int(time.time()))
        pod = {"restartPolicy": "Never", "automountServiceAccountToken": False,
               "containers": [{"name": "guard", "image": self.image, "command": ["python", "-B", "/opt/traffic-release/maintenance.py"],
                               "envFrom": main.get("envFrom", []), "env": env,
                               "resources": {"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"cpu": "250m", "memory": "256Mi"}}}]}
        if self.pull_secret:
            pod["imagePullSecrets"] = [{"name": self.pull_secret}]
        elif api["spec"]["template"]["spec"].get("imagePullSecrets"):
            pod["imagePullSecrets"] = api["spec"]["template"]["spec"]["imagePullSecrets"]
        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "traffic-mlops"},
               "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 3600, "ttlSecondsAfterFinished": 86400,
                        "template": {"spec": pod}}}
        self.save(guard_job=name)
        self.cluster.call("create", "-f", "-", "-o", "json", value=job)
        for _ in range(90):
            pods = self.cluster.call("get", "pods", "-l", "job-name=" + name, "-o", "json")["items"]
            if pods and pods[0].get("status", {}).get("phase") == "Running":
                self.save(guard_pod=pods[0]["metadata"]["name"])
                status = self.guard_status()
                if status["state"] == "held":
                    self.save(guard_held=True)
                    return
                if status["state"] == "failed":
                    raise RuntimeError("Maintenance guard could not be acquired.")
            if any(p.get("status", {}).get("phase") in {"Failed", "Succeeded"} for p in pods):
                raise RuntimeError("Maintenance guard terminated before acquiring its lock.")
            time.sleep(2)
        raise RuntimeError("Maintenance guard did not become ready.")

    def release_guard(self):
        self.guard_status(release=True)
        self.cluster.call("wait", "--for=condition=complete", "job/" + self.state["guard_job"], "--timeout=30s", json_output=False)
        logs = self.cluster.call("logs", self.state["guard_pod"], json_output=False)
        status = json.loads(logs.strip().splitlines()[-1])
        if status.get("maintenance") != "released" or status.get("data_unchanged") is not True:
            raise RuntimeError("Maintenance release/data continuity was not confirmed.")
        self.save(guard_held=False)

    def rollback(self):
        if self.guard_status().get("state") != "held":
            raise RuntimeError("Guard lost; automatic rollback is unsafe. Recover the recorded transaction.")
        plans = self.state["plans"]
        for name in reversed(self.state["patched"]):
            current = self.cluster.deployment(name)
            plan = plans[name]
            if current["metadata"]["uid"] == plan["uid"] and spec_hash(current) == plan["before"]:
                continue  # A timeout may have happened before the patch reached the API.
            if current["metadata"]["uid"] != plan["uid"] or spec_hash(current) != plan["after"]:
                raise RuntimeError("External deployment change prevents rollback: " + name)
            self.cluster.patch(current, plan["inverse"])
            self.cluster.wait(name)
            if spec_hash(self.cluster.deployment(name)) != plan["before"]:
                raise RuntimeError("Rollback specification differs: " + name)
        self.pause(self.state["dag_was_paused"])
        self.release_guard()
        self.save(rolled_back=True, recovery_required=False)

    def apply(self):
        if not self.image.startswith("ghcr.io/"):
            raise ValueError("Apply requires the published GHCR release image, not the Plan-only base image reference.")
        if self.state.get("started") and not (self.state.get("completed") or self.state.get("rolled_back")):
            raise RuntimeError("An interrupted delivery needs recovery; do not replay Apply.")
        values, readiness = self.inventory()
        if all(values[n]["spec"]["template"]["metadata"].get("annotations", {}).get(ANNOTATION) == self.release["release_id"] for n in self.release["baseline"]["application_deployments"]):
            self.health()
            self.save(completed=True, up_to_date=True)
            return {"up_to_date": True}
        _, _, plans = self.plan()
        self.save(started=True, completed=False, rolled_back=False, patched=[], plans=plans, dag_was_paused=readiness["dag_paused"])
        try:
            self.start_guard(values["traffic-api"])
            self.pause(True)
            for name in self.names:
                if self.guard_status().get("state") != "held":
                    raise RuntimeError("Maintenance guard was lost.")
                current = self.cluster.deployment(name)
                plan = plans[name]
                if current["metadata"]["uid"] != plan["uid"] or spec_hash(current) != plan["before"]:
                    raise RuntimeError("Deployment changed after review: " + name)
                if plan["before"] == plan["after"]:
                    continue
                self.save(patched=[*self.state["patched"], name])
                self.cluster.patch(current, plan["patch"])
                self.cluster.wait(name)
                if spec_hash(self.cluster.deployment(name)) != plan["after"]:
                    raise RuntimeError("Deployment differs from its validated specification.")
            proof = self.health()
            self.pause(self.state["dag_was_paused"])
            self.release_guard()
            self.save(completed=True, recovery_required=False, proof=proof)
            return {"completed": True, "commit": self.release["commit"], "release_id": self.release["release_id"]}
        except Exception:
            self.save(recovery_required=True)
            if self.state.get("guard_held"):
                try:
                    self.rollback()
                except Exception:
                    self.save(recovery_required=True)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("Plan", "Apply", "Verify", "Rollback"))
    for name in ("directory", "image", "commit", "journal", "kubectl", "context", "namespace-uid", "kubeconfig"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--pull-secret")
    args = parser.parse_args()
    operator = Delivery(Cluster(args.kubectl, args.context, args.namespace_uid, args.kubeconfig), args.directory, args.image, args.journal,
                        commit=args.commit, pull_secret=args.pull_secret)
    try:
        if args.action == "Plan":
            _, _, plans = operator.plan()
            print(json.dumps({"server_validated_not_applied": True, "image_pull_and_startup_verified": False,
                              "changes": {n: p["before"] != p["after"] for n, p in plans.items()}}))
        elif args.action == "Apply":
            print(json.dumps(operator.apply()))
        elif args.action == "Rollback":
            operator.rollback()
            print(json.dumps({"rolled_back": True}))
        else:
            print(json.dumps({"verified": True, "pods": operator.health()}))
        return 0
    except Exception as error:
        print(json.dumps({"delivery_failed": True, "error_type": type(error).__name__, "reason": str(error) if isinstance(error, (ValueError, RuntimeError)) else "Raw details omitted.",
                          "recovery_required": operator.state.get("recovery_required", False)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Small in-cluster batch client with namespace/ownership checks and bounded IO."""
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import requests

NODES = ("mlops-node-02", "mlops-node-03")
LABEL = "traffic.mlops/local-training"
MODELS = ("random_forest_lag", "xgboost_lag", "lightgbm_lag", "lstm", "gru")


def quantity(value):
    for suffix, scale in (("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024), ("G", 10**9), ("M", 10**6), ("K", 1000)):
        if value.endswith(suffix):
            return int(float(value[:-len(suffix)]) * scale)
    return int(value)


def cpu_cores(value):
    for suffix, scale in (("n", 1e-9), ("u", 1e-6), ("m", .001)):
        if value.endswith(suffix):
            return float(value[:-len(suffix)]) * scale
    return float(value)


def identity(context, model):
    if model not in MODELS:
        raise ValueError("Unknown local training model.")
    return hashlib.sha256(json.dumps([context, model], sort_keys=True).encode()).hexdigest()


def manifest(settings, context, model, prefix, bucket):
    from backend.app.services.training_policy import local_ready
    if not local_ready(settings):
        raise ValueError("Local training image/resources are not configured.")
    fingerprint = identity(context, model)
    labels = {LABEL: "true", "traffic.mlops/execution": fingerprint[:40]}
    environment = {"LOCAL_INPUT_PREFIX": prefix, "LOCAL_MODEL": model,
                   "LOCAL_BUCKET": bucket, "LOCAL_CONTEXT": json.dumps(context),
                   "TRAFFIC_LOCAL_CPU_THREADS": str(settings.local_training_cpu),
                   "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
                   "CUDA_VISIBLE_DEVICES": "-1"}
    for key in ("S3_ENDPOINT_URL", "AWS_DEFAULT_REGION"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    env = [{"name": k, "value": v} for k, v in environment.items()]
    for key, secret in (("AWS_ACCESS_KEY_ID", "MINIO_ROOT_USER"), ("AWS_SECRET_ACCESS_KEY", "MINIO_ROOT_PASSWORD")):
        env.append({"name": key, "valueFrom": {"secretKeyRef": {"name": "traffic-secrets", "key": secret}}})
    threads = str(settings.local_training_cpu)
    env.extend({"name": k, "value": threads} for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
               "NUMEXPR_NUM_THREADS", "TF_NUM_INTRAOP_THREADS", "TF_NUM_INTEROP_THREADS"))
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {
        "name": "local-train-" + fingerprint[:32], "namespace": settings.local_training_namespace,
        "labels": labels, "annotations": {"traffic.mlops/identity": fingerprint}}, "spec": {
        "backoffLimit": 0, "activeDeadlineSeconds": settings.local_training_deadline_seconds,
        "template": {"metadata": {"labels": labels}, "spec": {
            "serviceAccountName": settings.local_training_service_account, "automountServiceAccountToken": False,
            "restartPolicy": "Never", "terminationGracePeriodSeconds": 20,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "fsGroup": 10001},
            "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{
                "matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": list(NODES)}]}]}},
                "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
                    "labelSelector": {"matchLabels": {LABEL: "true"}}, "topologyKey": "kubernetes.io/hostname"}]}},
            "containers": [{"name": "training", "image": settings.local_training_image, "imagePullPolicy": "IfNotPresent",
                "command": ["python", "-u", "-B", "-m", "src.local_training.job"], "env": env,
                "resources": {"requests": {"cpu": threads, "memory": f"{settings.local_training_memory_mib}Mi", "ephemeral-storage": "4Gi"},
                              "limits": {"cpu": threads, "memory": f"{settings.local_training_memory_mib}Mi", "ephemeral-storage": "4Gi"}},
                "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                "volumeMounts": [{"name": "work", "mountPath": "/work"}, {"name": "tmp", "mountPath": "/tmp"}]}],
            "volumes": [{"name": "work", "emptyDir": {"sizeLimit": "3Gi"}}, {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}}]}}}}


class Kubernetes:
    def __init__(self, namespace):
        if namespace != "traffic-mlops":
            raise ValueError("Training is restricted to the application namespace.")
        self.namespace = namespace
        self.session = requests.Session()
        self.session.trust_env = False
        self.base = "https://kubernetes.default.svc"
        self.ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        self.token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")

    def call(self, method, path, *, document=None, missing=False, plain=False):
        response = self.session.request(method, self.base + path, json=document, timeout=(5, 20), verify=self.ca,
            headers={"Authorization": "Bearer " + self.token.read_text().strip()}, allow_redirects=False)
        try:
            if missing and response.status_code == 404:
                return None
            if response.status_code == 409 and method == "POST":
                return None  # Caller reads and validates the existing identity.
            if response.status_code not in {200, 201, 202}:
                raise RuntimeError(f"Kubernetes {method} failed ({response.status_code}).")
            if len(response.content) > 8 * 1024 * 1024:
                raise RuntimeError("Kubernetes response exceeded its byte budget.")
            return response.text if plain else response.json()
        finally:
            response.close()

    def path(self, name=""):
        if name and not name.startswith("local-train-"):
            raise ValueError("Unexpected training Job name.")
        return f"/apis/batch/v1/namespaces/{self.namespace}/jobs" + ("/" + quote(name, safe="") if name else "")

    def probe(self):
        self.call("GET", self.path() + "?limit=1")
        self.call("GET", f"/api/v1/namespaces/{self.namespace}/pods?limit=1")
        self.call("GET", "/api/v1/nodes")
        self.call("GET", "/apis/metrics.k8s.io/v1beta1/nodes")
        return True

    def get(self, name):
        return self.call("GET", self.path(name), missing=True)

    def create(self, desired):
        self.call("POST", self.path(), document=desired)
        return self.get(desired["metadata"]["name"])

    def pods(self, name):
        return self.call("GET", f"/api/v1/namespaces/{self.namespace}/pods?labelSelector=" + quote("job-name=" + name, safe=""))["items"]

    def logs(self, pod):
        name = quote(pod["metadata"]["name"], safe="")
        return self.call("GET", f"/api/v1/namespaces/{self.namespace}/pods/{name}/log?container=training", plain=True)

    def stop(self, name, uid):
        # Foreground deletion keeps the Job visible until its pods have stopped.
        current = self.get(name)
        if current is None:
            return not self.pods(name)
        if current["metadata"]["uid"] != uid:
            raise RuntimeError("Training Job ownership changed.")
        self.call("DELETE", self.path(name), document={"apiVersion": "v1", "kind": "DeleteOptions",
            "propagationPolicy": "Foreground", "preconditions": {"uid": uid}}, missing=True)
        return False

    def available(self, memory_mib, reserve_mib, cpu=1):
        # Fail closed when metrics are unavailable; do not infer capacity from CPU idle.
        nodes = self.call("GET", "/api/v1/nodes")["items"]
        metrics = self.call("GET", "/apis/metrics.k8s.io/v1beta1/nodes")["items"]
        usage = {}
        for metric in metrics:
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(metric["timestamp"])).total_seconds()
                if 0 <= age <= 90:
                    usage[metric["metadata"]["name"]] = (quantity(metric["usage"]["memory"]), cpu_cores(metric["usage"]["cpu"]))
            except (KeyError, TypeError, ValueError):
                continue
        occupied = {p.get("spec", {}).get("nodeName") for p in self.call("GET",
            f"/api/v1/namespaces/{self.namespace}/pods?labelSelector=" + quote(LABEL + "=true", safe=""))["items"]
            if p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}}
        # The Job controller may not have created its Pod yet. Count the
        # reserved hostname as soon as a Job exists, including Pending Jobs.
        jobs = self.call("GET", self.path() + "?labelSelector=" + quote(LABEL + "=true", safe=""))["items"]
        for job in jobs:
            affinity = job["spec"]["template"]["spec"].get("affinity", {}).get("nodeAffinity", {})
            for term in affinity.get("requiredDuringSchedulingIgnoredDuringExecution", {}).get("nodeSelectorTerms", []):
                for expression in term.get("matchExpressions", []):
                    if expression.get("key") == "kubernetes.io/hostname" and expression.get("operator") == "In":
                        occupied.update(expression.get("values", []))
        eligible = []
        for node in nodes:
            name = node["metadata"]["name"]
            conditions = {c["type"]: c["status"] for c in node["status"].get("conditions", [])}
            if (name in NODES and name not in occupied and name in usage and not node["spec"].get("unschedulable")
                    and conditions.get("Ready") == "True" and conditions.get("MemoryPressure") == "False"
                    and conditions.get("DiskPressure") == "False"
                    and quantity(node["status"]["allocatable"]["memory"]) - usage[name][0] >= (memory_mib + reserve_mib) * 1024**2
                    and cpu_cores(node["status"]["allocatable"]["cpu"]) - usage[name][1] >= cpu + .5):
                eligible.append(name)
        return eligible

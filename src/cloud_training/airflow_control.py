"""Airflow polling protocol. No model fitting or credentials in task logs."""
import os
import re
import time
import uuid

import requests

URLS = {"tree": "http://traffic-training-tree:8000", "recurrent": "http://traffic-training-worker:8000"}
NODES = {"tree": "mlops-node-02", "recurrent": "mlops-node-03"}


class WorkerUnavailable(RuntimeError):
    def __init__(self, status=None):
        super().__init__("Training worker request could not be confirmed; provider details omitted.")
        self.status = status


def post(role, path, payload=None):
    token = os.environ.get("INTERNAL_TRAINING_TOKEN")
    if role not in URLS or not token:
        raise WorkerUnavailable()
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.post(URLS[role] + "/api/v1/internal" + path,
                              headers={"X-Internal-Token": token}, json=payload,
                              timeout=(10, 25), allow_redirects=False) as response:
                if response.status_code != 200 or len(response.content) > 1024 * 1024:
                    raise WorkerUnavailable(response.status_code)
                document = response.json()
        worker = document.get("execution_worker", {})
        prefix = "traffic-training-tree-" if role == "tree" else "traffic-training-worker-"
        if (worker.get("role") != role or worker.get("node") != NODES[role]
                or worker.get("runtime_revision") != "branch-execution-v1"
                or worker.get("control_revision") != "cloud-training-v1"
                or not isinstance(worker.get("pod"), str) or not worker["pod"].startswith(prefix)):
            raise WorkerUnavailable()
        return document
    except (requests.RequestException, ValueError, TypeError):
        raise WorkerUnavailable() from None


def check_receipt(receipt, conf, branch):
    keys = {"version", "region_id", "run_id", "branch", "bundle_id", "manifest_sha256"}
    if (not isinstance(receipt, dict) or set(receipt) != keys or type(receipt["version"]) is not int
            or receipt["version"] != 1 or receipt["region_id"] != conf["region_id"]
            or receipt["run_id"] != conf["training_run_id"] or receipt["branch"] != branch
            or not isinstance(receipt["manifest_sha256"], str) or not re.fullmatch("[a-f0-9]{64}", receipt["manifest_sha256"])):
        raise WorkerUnavailable()
    if str(uuid.UUID(receipt["bundle_id"])) != receipt["bundle_id"]:
        raise WorkerUnavailable()
    return receipt


def request_stop(conf, *, send=post):
    run_id = conf.get("training_run_id")
    if str(uuid.UUID(str(run_id))) != run_id:
        raise ValueError("Invalid controlled run identity.")
    for role in URLS:
        try:
            send(role, f"/training-runs/{run_id}/control/stop")
        except Exception:
            pass  # Worker/cloud leases still expire. Never claim stop confirmed.


def train(conf, branch, *, send=post, sleep=time.sleep, clock=time.monotonic, log=print):
    if branch not in {"tree", "lstm", "gru"} or conf.get("training_backend") not in {"local", "kaggle"}:
        raise ValueError("Invalid controlled training branch/backend.")
    role = "tree" if branch == "tree" else "recurrent"
    path = f"/training-runs/{conf['training_run_id']}/control/{branch}"
    deadline, cursor = clock() + 8 * 3600, 0
    log("Training backend=" + conf["training_backend"] + "; branch=" + branch)
    try:
        # Repeating start is safe: the worker returns the same durable attempt.
        for retry in range(6):
            try:
                response = send(role, path + "/start")
                break
            except WorkerUnavailable as error:
                if retry == 5 or error.status not in {None, 503}:
                    raise
                sleep(2)
        attempt = response["attempt_id"]
        if str(uuid.UUID(attempt)) != attempt:
            raise WorkerUnavailable()
        while clock() < deadline:
            if (response.get("training_run_id") != conf["training_run_id"] or response.get("region_id") != conf["region_id"]
                    or response.get("dataset_id") != conf["dataset_id"] or response.get("attempt_id") != attempt
                    or response.get("backend") != conf["training_backend"]):
                raise WorkerUnavailable()
            for event in response.get("events", []):
                sequence = event.get("sequence")
                if type(sequence) is not int or sequence <= cursor:
                    continue
                cursor = sequence
                code = event.get("code")
                if not isinstance(code, str) or not re.fullmatch("[a-z_]{1,60}", code):
                    continue
                details = event.get("details") or {}
                counters = [f"{key}={details[key]}" for key in ("fold", "folds", "epoch", "epochs")
                            if type(details.get(key)) is int]
                # Only normalized protocol fields; never raw SDK/HTTP errors.
                log("Training " + branch + ": " + code + ("; " + ", ".join(counters) if counters else ""))
            if response.get("cancel_requested") or response.get("state") in {"failed", "cancelling"}:
                raise RuntimeError("Training failed; stopping the remaining work. No backend fallback was started.")
            if response.get("branch_state") in {"ready", "skipped"}:
                receipt = check_receipt(response.get("artifact_receipt"), conf, branch)
                return {"training_run_id": conf["training_run_id"], "branch": branch,
                        "status": response["branch_state"], "artifact_receipt": receipt,
                        "backend": conf["training_backend"], "execution_worker": response["execution_worker"]}
            sleep(3)
            for retry in range(3):
                try:
                    response = send(role, path + "/status", {"attempt_id": attempt, "after_sequence": cursor})
                    break
                except WorkerUnavailable as error:
                    if retry == 2 or error.status not in {None, 503} or clock() >= deadline:
                        raise
                    sleep(retry + 1)
        raise RuntimeError("Training monitoring exceeded its deadline.")
    except Exception:
        request_stop(conf, send=send)
        raise


def finalize(conf, receipts, *, send=post, sleep=time.sleep):
    if set(receipts) != {"tree", "lstm", "gru"}:
        raise ValueError("All three branch receipts are required.")
    for branch, receipt in receipts.items():
        check_receipt(receipt, conf, branch)
    try:
        for retry in range(3):
            try:
                return send("tree", f"/training-runs/{conf['training_run_id']}/control/finalize", {"artifact_receipts": receipts})
            except WorkerUnavailable as error:
                if retry == 2 or error.status not in {None, 503}:
                    raise
                sleep(2)
    except Exception:
        request_stop(conf, send=send)
        raise

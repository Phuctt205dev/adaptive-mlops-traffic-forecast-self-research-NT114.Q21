"""One worker operation, durable cancellation and scoped artifact cleanup.

Imported only by the training worker entrypoint. HTTP start is short; the
controller owns a real subprocess/remote execution until it is stopped.
"""
import copy
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from sqlalchemy import select

from backend.app.core.errors import ApplicationError
from backend.app.db.models import TrainingRun
from backend.app.db.training_execution import TrainingExecution
from backend.app.services import training_execution as state
from src.cloud_training.artifacts import read_bundle
from src.cloud_training.kaggle_execution import KaggleExecution, TrainingCancelled, initial_ledger
from src.cloud_training.kaggle_transport import ProviderError, SDKTransport
from src.cloud_training.package import configuration_digest, prepare_package
from src.cloud_training.process import ExecutionProcess, cleanup_execution, execution_directory


def delete_prefix(store, prefix):
    """Only callers deriving UUID-owned staging prefixes may invoke this."""
    if not prefix.endswith("/"):
        raise ValueError("Cleanup requires an exact directory prefix.")
    count, token = 0, None
    while True:
        arguments = {"Bucket": store.bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            arguments["ContinuationToken"] = token
        response = store.client.list_objects_v2(**arguments)
        keys = [item["Key"] for item in response.get("Contents", [])]
        if any(not key.startswith(prefix) for key in keys):
            raise ValueError("Storage returned an out-of-scope cleanup key.")
        count += len(keys)
        if count > 20000:
            raise ValueError("Cleanup exceeded its object budget.")
        if keys:
            result = store.client.delete_objects(Bucket=store.bucket, Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True})
            if result.get("Errors"):
                return False
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
        if not token:
            raise ValueError("Storage cleanup pagination was incomplete.")
    return not store.client.list_objects_v2(Bucket=store.bucket, Prefix=prefix, MaxKeys=1).get("Contents")


class WorkerController:
    def __init__(self, sessions, training, bridge, *, role, identity, root, project,
                 username="", transport=None, operation_lock=None, barrier_factory=None, status_state=None):
        self.sessions, self.training, self.bridge = sessions, training, bridge
        self.role, self.identity, self.root = role, identity, Path(root)
        self.project, self.username = Path(project), username
        self.transport = transport or SDKTransport(username, repository=self.project)
        self.barrier_factory = barrier_factory
        self.status_state = status_state
        self.worker = identity["pod"] + ":" + str(uuid.uuid4())
        self.operation_lock = operation_lock or threading.Lock()
        self.handles, self.handles_lock = {}, threading.RLock()
        self.shutdown = threading.Event()
        self.watcher = None

    def owns(self, branch):
        return (self.role == "tree" and branch == "tree") or (self.role == "recurrent" and branch in {"lstm", "gru"})

    def save_provider(self, run_id, branch, attempt, value):
        with self.sessions() as db:
            state.update_provider(db, run_id, branch, attempt, value)

    def start(self, run_id, branch):
        if not self.owns(branch):
            raise ApplicationError("worker_role_mismatch", "Wrong worker role for this branch.", 409)
        key = (str(run_id), branch)
        with self.handles_lock, self.sessions() as db:
            run, dataset, _region = self.training._get_training_context(db, run_id)
            if (run.configuration_json or {}).get("_execution_protocol") != 2:
                raise ApplicationError("training_protocol_mismatch", "This endpoint requires a controlled training run.", 409)
            record = state.execution(db, run_id)
            state.assert_open(record)
            current = record.branches_json[branch]
            if current.get("attempt_id"):
                if current["state"] not in {"ready", "skipped"} and key not in self.handles:
                    raise ApplicationError("training_recovery_pending", "This attempt needs recovery before more work.", 409)
                return self.response(db, run_id, branch)
            pending = db.scalars(select(TrainingExecution).where(TrainingExecution.cancel_requested.is_(True),
                                 TrainingExecution.state != "completed")).all()
            if any(self.owns(name) and item.get("attempt_id") and not item.get("cleanup_confirmed")
                   for pending_run in pending for name, item in pending_run.branches_json.items()):
                raise ApplicationError("training_recovery_pending", "An earlier attempt is still being stopped or cleaned.", 503)
            if not self.operation_lock.acquire(blocking=False):
                raise ApplicationError("training_worker_busy", "This worker already owns a training operation.", 503)
            try:
                current = state.claim_branch(db, run_id, branch, self.worker)
                if self.status_state is not None:
                    self.status_state["busy"] = True
                    self.status_state["operation_count"] += 1
                handle = {"attempt_id": current["attempt_id"], "process": None, "provider": None,
                          "lease_owned": True, "stopped": False, "thread": None, "failed": False, "barrier": None}
                self.handles[key] = handle
                thread = threading.Thread(target=self.execute, args=(run_id, branch, handle), daemon=True)
                handle["thread"] = thread
                thread.start()
                return self.response(db, run_id, branch)
            except Exception:
                self.handles.pop(key, None)
                self.operation_lock.release()
                if self.status_state is not None:
                    self.status_state["busy"] = False
                if current.get("attempt_id"):
                    state.fail_run(db, run_id, branch, current["attempt_id"], "worker_restarted")
                raise

    def response(self, db, run_id, branch, after=0):
        run, dataset, _ = self.training._get_training_context(db, run_id)
        record = state.execution(db, run_id)
        result = state.snapshot(record, run, dataset)
        result["events"] = [event for event in result["events"] if event["sequence"] > after]
        result.update(attempt_id=record.branches_json[branch]["attempt_id"],
                      branch_state=record.branches_json[branch]["state"],
                      artifact_receipt=record.branches_json[branch].get("receipt"),
                      execution_worker={**self.identity, "control_revision": "cloud-training-v1"})
        # Provider journal belongs to the worker, not the browser/Airflow log.
        result["branches"] = {name: {key: value for key, value in entry.items() if key != "provider"}
                              for name, entry in result["branches"].items()}
        return result

    def status(self, run_id, branch, attempt, after):
        if not self.owns(branch) or type(after) is not int or after < 0:
            raise ApplicationError("training_poll_invalid", "Invalid training polling request.", 422)
        with self.sessions() as db:
            state.heartbeat(db, run_id, branch, attempt)
            return self.response(db, run_id, branch, after)

    def cancelled(self, run_id, branch, attempt):
        with self.sessions() as db:
            record = state.execution(db, run_id)
            current = state.matching_attempt(record, branch, attempt)
            if record.cancel_requested or record.state not in {"running", "queued"}:
                return True
            if current.get("airflow_lease_until", 0) < time.time():
                state.fail_run(db, run_id, branch, attempt, "monitoring_timeout")
                return True
            return self.shutdown.is_set()

    def progress(self, run_id, branch, attempt, event):
        with self.sessions() as db:
            state.progress(db, run_id, branch, attempt, event)

    def execute(self, run_id, branch, handle):
        attempt = handle["attempt_id"]
        cancelled = lambda: self.cancelled(run_id, branch, attempt)
        progress = lambda event: self.progress(run_id, branch, attempt, event)
        directory = execution_directory(self.root, str(run_id), branch, attempt)
        try:
            if self.barrier_factory:
                barrier = self.barrier_factory()
                barrier.__enter__()
                handle["barrier"] = barrier
            directory.mkdir(parents=True, exist_ok=False)
            with self.sessions() as db:
                run, dataset, _ = self.training._get_training_context(db, run_id)
                configuration = copy.deepcopy(run.configuration_json)
                context = {"run_id": str(run.id), "region_id": str(run.region_id), "dataset_id": str(run.dataset_id),
                           "branch": branch, "attempt_id": attempt,
                           "configuration_sha256": configuration_digest(configuration)}
                backend = state.execution(db, run_id).backend
                skip = not any(state.MODEL_BRANCH.get(name) == branch for name in configuration["selected_models"])
                profile = None
                if backend == "kaggle" and not skip:
                    packages = ["scikit-learn", "joblib", "pandas"]
                    if branch == "tree" and "xgboost_lag" in configuration["selected_models"]:
                        packages.append("xgboost")
                    if branch == "tree" and "lightgbm_lag" in configuration["selected_models"]:
                        packages.append("lightgbm")
                    profile = {name: importlib.metadata.version(name) for name in packages}
                    if configuration.get("shap", {}).get("enabled", True):
                        profile["shap"] = importlib.metadata.version("shap")
                if not skip:
                    source = self.training._ensure_dataset_downloaded(dataset, run)
                    snapshot = prepare_package(self.project, source, configuration, context, directory / "input", dataset.sha256, profile)
            if cancelled():
                raise TrainingCancelled("Training stopped before execution.")
            if skip:
                # Explicit empty receipt preserves the DAG/bridge contract.
                # No upload, notebook, GPU allocation or local fit is needed.
                result = {"branch": branch, "status": "skipped", "candidates": []}
                handle["stopped"] = True
                self.save_provider(run_id, branch, attempt, {"backend": backend, "skipped": True, "started": False})
            elif backend == "kaggle":
                if not self.username or not self.training.get_settings().cloud_training_enabled:
                    raise ApplicationError("cloud_training_not_ready", "Cloud execution is not configured on this worker.", 503)
                ledger = initial_ledger(snapshot, self.username)
                self.save_provider(run_id, branch, attempt, ledger)
                provider = KaggleExecution(self.transport, directory, ledger,
                    lambda value: self.save_provider(run_id, branch, attempt, value))
                handle["provider"] = provider
                result = provider.run(progress, cancelled)
                handle["stopped"] = True
                if not provider.cleanup():
                    raise RuntimeError("Cloud staging cleanup was not confirmed.")
            else:
                self.save_provider(run_id, branch, attempt, {"backend": "local", "started": False})
                child = ExecutionProcess([sys.executable, "-u", "-B", str(self.project / "scripts/cloud_training/execute_branch.py"),
                    "--job", str(directory / "input/job.json"), "--dataset", str(directory / "input/dataset.csv"),
                    "--output", str(directory / "output")], context, cwd=self.project)
                handle["process"] = child
                self.save_provider(run_id, branch, attempt, {"backend": "local", "started": True,
                    "pid": child.process.pid, "owner": self.worker, "directory": str(directory)})
                deadline = time.monotonic() + 8 * 3600
                while True:
                    if cancelled():
                        raise TrainingCancelled("Training cancellation requested.")
                    for event in child.drain():
                        progress(event)
                    if child.reader_failed or time.monotonic() >= deadline:
                        raise RuntimeError("Local execution monitoring failed.")
                    code = child.returncode()
                    if code is not None:
                        for event in child.drain():
                            progress(event)
                        if code != 0:
                            raise RuntimeError("The ML child exited without a valid result.")
                        child.stop()
                        handle["stopped"] = True
                        break
                    self.shutdown.wait(.5)
                result = read_bundle(directory / "output", context)
                self.save_provider(run_id, branch, attempt, {"backend": "local", "started": True, "stopped": True})
            if cancelled():
                raise TrainingCancelled("Training stopped before result publication.")
            self.verify_serving(directory, context, result, handle, cancelled)
            with self.sessions() as db:
                # Serialize publication against a concurrent sibling failure.
                record = state.execution(db, run_id, lock=True)
                state.assert_open(record)
                run = db.get(TrainingRun, run_id)
                receipt = self.bridge.publish_branch(run, branch, result)
                state.branch_ready(db, run_id, branch, attempt, receipt, skipped=result.get("status") == "skipped")
            cleanup_execution(self.root, str(run_id), branch, attempt)
        except Exception as error:
            handle["failed"] = True
            if handle["provider"] is not None and isinstance(error, ProviderError):
                # Keep only normalized technical facts, never SDK exception text.
                try:
                    handle["provider"].save(last_failure={"operation": error.operation, "category": error.category,
                                                           "http_status": error.http_status})
                except Exception:
                    pass  # Diagnostics must not prevent the fail/stop fence.
            if handle["process"] is not None:
                handle["stopped"] = handle["process"].stop()
            with self.sessions() as db:
                db.rollback()
                state.fail_run(db, run_id, branch, attempt,
                               "cloud_unavailable" if handle["provider"] is not None and not (
                                   isinstance(error, ProviderError) and error.category == "ml_execution_failed") else "training_failed")
            # Watcher retries network/cleanup failures without making a new fit.
        finally:
            if handle["stopped"]:
                self.release(handle)

    def verify_serving(self, directory, context, result, handle, cancelled):
        candidates = result.get("candidates", [])
        if not candidates:
            return
        entries = []
        for candidate in candidates:
            if not candidate.get("compatibility_file"):
                raise RuntimeError("The ML result lacks serving compatibility samples.")
            entries.append({key: candidate.get(key) for key in (
                "model_file", "model_family", "preprocessor_file", "compatibility_file")})
        request = directory / "serving-check.json"
        request.write_text(json.dumps(entries), encoding="utf-8")
        child = ExecutionProcess([sys.executable, "-u", "-B", str(self.project / "scripts/cloud_training/verify_execution.py"),
                                  "--request", str(request)], context, cwd=self.project)
        handle["process"], handle["stopped"] = child, False
        deadline = time.monotonic() + 180
        try:
            while child.returncode() is None:
                if cancelled() or time.monotonic() >= deadline:
                    raise RuntimeError("CPU compatibility verification was interrupted.")
                self.shutdown.wait(.25)
            if child.returncode() != 0:
                raise RuntimeError("The saved model could not be reloaded for serving.")
        finally:
            handle["stopped"] = child.stop()

    def release(self, handle):
        with self.handles_lock:
            if handle.get("barrier") is not None:
                barrier = handle["barrier"]
                handle["barrier"] = None
                try:
                    barrier.__exit__(None, None, None)
                except Exception:
                    handle["failed"] = True
            if handle["lease_owned"]:
                handle["lease_owned"] = False
                self.operation_lock.release()
                if self.status_state is not None:
                    self.status_state["busy"] = False

    def prefixes(self, run, branch):
        region, run_id = str(uuid.UUID(str(run.region_id))), str(uuid.UUID(str(run.id)))
        if branch not in state.BRANCHES:
            raise ValueError("Invalid cleanup branch.")
        return (self.bridge.store.prefix + f"/regions/{region}/runs/{run_id}/branches/{branch}/",
                f"models/distributed-serving/v1/{region}/{run_id}/")

    def sweep(self, *, recovery=False):
        with self.sessions() as db:
            records = db.scalars(select(TrainingExecution).where(TrainingExecution.dismissed.is_(False),
                TrainingExecution.state.in_(["queued", "running", "finalizing", "cancelling", "completed"]))).all()
            ids = [record.training_run_id for record in records]
        for run_id in ids:
            for branch in state.BRANCHES:
                if not self.owns(branch):
                    continue
                with self.sessions() as db:
                    record = state.execution(db, run_id)
                    current = copy.deepcopy(record.branches_json[branch])
                    if not current.get("attempt_id"):
                        continue
                    key, attempt = (str(run_id), branch), current["attempt_id"]
                    with self.handles_lock:
                        handle = self.handles.get(key)
                    running_thread = handle and handle["thread"] and handle["thread"].is_alive()
                    if (recovery and record.state not in {"completed", "failed"}
                            and current.get("worker") != self.worker):
                        state.fail_run(db, run_id, branch, attempt, "worker_restarted")
                        record = state.execution(db, run_id)
                    if handle and handle["failed"] and not running_thread and record.state not in {"completed", "failed"}:
                        state.fail_run(db, run_id, branch, attempt)
                        record = state.execution(db, run_id)
                    if not running_thread and current["state"] == "running" and not record.cancel_requested:
                        state.fail_run(db, run_id, branch, attempt, "worker_restarted")
                        record = state.execution(db, run_id)
                    if record.state != "completed" and not record.cancel_requested:
                        continue
                    if running_thread or (record.cancel_requested and current.get("cleanup_confirmed")):
                        continue
                    provider_journal = current.get("provider") or {"backend": record.backend}
                    if record.state == "completed" and provider_journal.get("staging_cleaned"):
                        continue
                    run = db.get(TrainingRun, run_id)
                    exchange, serving = self.prefixes(run, branch)
                    completed = record.state == "completed"
                try:
                    directory = execution_directory(self.root, str(run_id), branch, attempt)
                    stopped = bool(handle and handle["stopped"])
                    if provider_journal["backend"] == "kaggle" and provider_journal.get("snapshot"):
                        directory.mkdir(parents=True, exist_ok=True)
                        provider = KaggleExecution(self.transport, directory, provider_journal,
                            lambda value, r=run_id, b=branch, a=attempt: self.save_provider(r, b, a, value))
                        stopped = provider.stop()
                        cleaned_cloud = stopped and provider.cleanup()
                    else:
                        # A local child in a dead container cannot survive. In
                        # a same-container supervisor restart, inspect its exact
                        # proc command before signalling the owned process group.
                        stopped = stopped or not provider_journal.get("started") or provider_journal.get("stopped", False)
                        pid = provider_journal.get("pid")
                        if not stopped and sys.platform == "linux" and type(pid) is int:
                            proc = Path("/proc") / str(pid)
                            if not proc.exists():
                                stopped = True
                            else:
                                import signal
                                command = (proc / "cmdline").read_bytes().split(b"\0")
                                if str(directory / "input/job.json").encode() not in command or os.getpgid(pid) != pid:
                                    raise RuntimeError("Local recovery process ownership is unconfirmed.")
                                os.killpg(pid, signal.SIGTERM)
                        cleaned_cloud = True
                    cleaned = False
                    if stopped and cleaned_cloud:
                        cleaned = delete_prefix(self.bridge.store, exchange)
                        if not completed and branch == "tree":
                            cleaned = delete_prefix(self.bridge.store, serving) and cleaned
                            from backend.app.services.training_tracking import TrainingTracker
                            cleaned = TrainingTracker().cleanup(run) and cleaned
                        cleaned = cleanup_execution(self.root, str(run_id), branch, attempt) and cleaned
                    if not completed:
                        with self.sessions() as db:
                            state.acknowledge_stop(db, run_id, branch, attempt, stopped=bool(stopped), cleaned=bool(cleaned))
                    elif cleaned:
                        self.save_provider(run_id, branch, attempt, {**provider_journal, "staging_cleaned": True})
                    if stopped and handle:
                        self.release(handle)
                    if cleaned:
                        with self.handles_lock:
                            self.handles.pop(key, None)
                except Exception:
                    # Retain cancelling/red state and retry. No raw SDK, path,
                    # token or dataset value enters application logs.
                    continue

    def boot(self):
        self.root.mkdir(parents=True, exist_ok=True)
        # Fence old attempts before accepting HTTP, but never block the server
        # startup on Kaggle/network cleanup. The watcher reconciles those jobs.
        with self.sessions() as db:
            ids = list(db.scalars(select(TrainingExecution.training_run_id).where(
                TrainingExecution.state.in_(["running", "finalizing", "queued", "cancelling"]))))
        for run_id in ids:
            with self.sessions() as db:
                record = state.execution(db, run_id)
                for branch, current in record.branches_json.items():
                    if self.owns(branch) and current.get("attempt_id") and current.get("worker") != self.worker:
                        state.fail_run(db, run_id, branch, current["attempt_id"], "worker_restarted")
                        break
        def watch():
            while not self.shutdown.wait(2):
                try:
                    self.sweep()
                except Exception:
                    pass
        self.watcher = threading.Thread(target=watch, daemon=True)
        self.watcher.start()

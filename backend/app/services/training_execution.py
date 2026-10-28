"""One durable run state for workers, Airflow and the read-only monitor.

All mutations lock the execution row in PostgreSQL. ML configuration is never
changed by progress events. A failed run can never pass the publication gate.
"""
import copy
from datetime import UTC, datetime
import math
import re
import uuid
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.db.models import Dataset, TrainingRun, TrainingRunStatus
from backend.app.db.training_execution import TrainingExecution


MODEL_NAMES = {"random_forest_lag": "Random Forest", "xgboost_lag": "XGBoost",
               "lightgbm_lag": "LightGBM", "lstm": "LSTM", "gru": "GRU"}
MODEL_BRANCH = {key: "tree" if key.endswith("_lag") else key for key in MODEL_NAMES}
BRANCHES = ("tree", "lstm", "gru")
MAX_EVENTS = 500
ERROR_MESSAGES = {
    "training_failed": "Training failed. The remaining work in this run is being stopped.",
    "cloud_unavailable": "Kaggle could not execute the selected job. No local fallback was started.",
    "monitoring_timeout": "Training status could not be confirmed within the monitoring time limit.",
    "artifact_invalid": "Training output did not pass validation and was not registered.",
    "publication_failed": "Training output could not be published safely.",
    "worker_restarted": "The worker restarted before this operation could be confirmed.",
    "cleanup_failed": "Temporary output cleanup has not been confirmed.",
    "dispatch_failed": "Airflow could not confirm submission. This run is closed to new training work.",
}


def now():
    return datetime.now(UTC)


def execution(db: Session, run_id, *, lock=False):
    statement = select(TrainingExecution).where(TrainingExecution.training_run_id == run_id)
    if lock:
        statement = statement.with_for_update()
    value = db.scalar(statement.execution_options(populate_existing=True))
    if value is None:
        raise ApplicationError("training_execution_not_found", "Training execution was not found.", 404)
    return value


def add_event(record, code, *, level="info", model=None, branch=None, details=None):
    record.sequence += 1
    event = {"sequence": record.sequence, "time": now().isoformat(), "level": level,
             "code": code, "model": model, "branch": branch, "details": details or {}}
    record.events_json = [*(record.events_json or []), event][-MAX_EVENTS:]
    record.updated_at = now()


def create_execution(db, run):
    config = run.configuration_json or {}
    backend = config.get("training_backend", "local")
    selected = config.get("selected_models", list(MODEL_NAMES))
    if backend not in {"local", "kaggle"} or not selected or any(key not in MODEL_NAMES for key in selected):
        raise ApplicationError("training_configuration_invalid", "Invalid training backend or model selection.", 422)
    record = TrainingExecution(training_run_id=run.id, backend=backend, state="queued",
                               cancel_requested=False, dismissed=False, sequence=0,
                               models_json={key: {"name": MODEL_NAMES[key], "state": "waiting", "stage": "queued"}
                                            for key in selected},
                               branches_json={branch: {"state": "waiting", "attempt_id": None, "last_event_seq": 0,
                                                       "cleanup_confirmed": False} for branch in BRANCHES},
                               events_json=[])
    db.add(record)
    add_event(record, "run_queued", details={"backend": backend})
    return record


def assert_open(record):
    if record.cancel_requested or record.state not in {"queued", "running", "finalizing"}:
        raise ApplicationError("training_run_closed", "This training run no longer accepts work or output.", 409)


def claim_branch(db, run_id, branch, worker):
    if branch not in BRANCHES or not isinstance(worker, str) or not 1 <= len(worker) <= 180:
        raise ApplicationError("training_branch_invalid", "Invalid training branch or worker identity.", 422)
    record = execution(db, run_id, lock=True)
    assert_open(record)
    branches = copy.deepcopy(record.branches_json)
    current = branches[branch]
    # Repeated start requests return the existing attempt; they never create a
    # second fit. A restarted owner needs explicit recovery, not a fresh claim.
    if current["attempt_id"]:
        if current.get("worker") != worker:
            raise ApplicationError("training_owner_changed", "The existing attempt needs recovery by its owner.", 409)
        db.commit()
        return copy.deepcopy(current)
    attempt = str(uuid.uuid4())
    current.update(state="running", attempt_id=attempt, worker=worker, started_at=now().isoformat(),
                   airflow_lease_until=time.time() + 180)
    record.branches_json = branches
    record.state = "running"
    run = db.get(TrainingRun, run_id)
    run.status = TrainingRunStatus.TRAINING
    run.started_at = run.started_at or now()
    add_event(record, "branch_started", branch=branch, details={"backend": record.backend, "worker": worker})
    db.commit()
    return copy.deepcopy(current)


def matching_attempt(record, branch, attempt):
    if (branch not in BRANCHES or not isinstance(attempt, str) or not attempt
            or record.branches_json[branch].get("attempt_id") != attempt):
        raise ApplicationError("training_attempt_mismatch", "Training output belongs to a different attempt.", 409)
    return record.branches_json[branch]


def update_provider(db, run_id, branch, attempt, provider):
    """Trusted worker journal. The run/attempt and selected backend cannot change."""
    record = execution(db, run_id, lock=True)
    current = matching_attempt(record, branch, attempt)
    if (not isinstance(provider, dict) or provider.get("backend") != record.backend
            or len(__import__("json").dumps(provider, allow_nan=False)) > 128 * 1024):
        raise ApplicationError("training_provider_invalid", "Invalid execution provider journal.", 422)
    branches = copy.deepcopy(record.branches_json)
    branches[branch]["provider"] = copy.deepcopy(provider)
    record.branches_json, record.updated_at = branches, now()
    if record.state == "completed" and all(item.get("provider", {}).get("staging_cleaned") for item in branches.values()):
        record.dismissed = True  # Stop sweeping completed runs after scoped cleanup.
    db.commit()


def heartbeat(db, run_id, branch, attempt):
    record = execution(db, run_id, lock=True)
    matching_attempt(record, branch, attempt)
    if not record.cancel_requested and record.state in {"queued", "running", "finalizing"}:
        branches = copy.deepcopy(record.branches_json)
        branches[branch]["airflow_lease_until"] = time.time() + 180
        record.branches_json = branches
    db.commit()
    return execution(db, run_id)


def request_stop(db, run_id, code="monitoring_timeout"):
    record = execution(db, run_id, lock=True)
    for branch, current in record.branches_json.items():
        if current.get("attempt_id"):
            return fail_run(db, run_id, branch, current["attempt_id"], code)
    return fail_dispatch(db, run_id)


def progress(db, run_id, branch, attempt, event):
    record = execution(db, run_id, lock=True)
    assert_open(record)
    current = matching_attempt(record, branch, attempt)
    if current["state"] != "running":
        raise ApplicationError("training_branch_closed", "The training branch no longer accepts progress.", 409)
    if (not isinstance(event, dict) or set(event) - {"sequence", "model", "stage", "fold", "folds", "epoch", "epochs", "loss"}
            or type(event.get("sequence")) is not int or event["sequence"] < 1):
        raise ApplicationError("training_progress_invalid", "Invalid training progress event.", 422)
    if event["sequence"] <= current.get("last_event_seq", 0):
        db.commit()
        return False
    model, stage = event.get("model"), event.get("stage")
    allowed_stages = {"preparing", "waiting_cloud", "model_started", "fold_started", "epoch_started",
                      "epoch_completed", "fold_completed", "model_cv_completed", "final_fit_started",
                      "model_evaluated", "saving", "explaining"}
    if (stage not in allowed_stages or model not in record.models_json or MODEL_BRANCH[model] != branch):
        raise ApplicationError("training_progress_invalid", "Progress model/branch/stage does not match the run.", 422)
    details = {}
    for key in ("fold", "folds", "epoch", "epochs"):
        if key in event:
            if type(event[key]) is not int or not 0 <= event[key] <= 100000:
                raise ApplicationError("training_progress_invalid", "Invalid progress counter.", 422)
            details[key] = event[key]
    if "loss" in event:
        if type(event["loss"]) not in (int, float) or not math.isfinite(event["loss"]):
            raise ApplicationError("training_progress_invalid", "Invalid progress loss.", 422)
        details["loss"] = float(event["loss"])
    models, branches = copy.deepcopy(record.models_json), copy.deepcopy(record.branches_json)
    if (models[model]["state"] == "evaluated" and stage not in {
            "model_evaluated", "final_fit_started", "saving", "explaining"}):
        raise ApplicationError("training_model_closed", "A completed model cannot regress to running.", 409)
    state = "evaluated" if stage in {"model_evaluated", "model_cv_completed"} else "waiting" if stage == "waiting_cloud" else "running"
    if stage in {"model_started", "model_cv_completed", "final_fit_started", "model_evaluated"}:
        for key in ("fold", "folds", "epoch", "epochs", "loss"):
            models[model].pop(key, None)
    elif stage == "fold_started":
        for key in ("epoch", "epochs", "loss"):
            models[model].pop(key, None)
    models[model].update(state=state, stage=stage,
                         **details, updated_at=now().isoformat())
    if stage in {"model_evaluated", "model_cv_completed"}:
        models[model]["evaluation_completed"] = True
    branches[branch]["last_event_seq"] = event["sequence"]
    record.models_json, record.branches_json = models, branches
    add_event(record, stage, model=model, branch=branch, details=details)
    db.commit()
    return True


def branch_ready(db, run_id, branch, attempt, receipt, *, skipped=False):
    record = execution(db, run_id, lock=True)
    assert_open(record)
    current = matching_attempt(record, branch, attempt)
    expected = {key for key in record.models_json if MODEL_BRANCH[key] == branch}
    if skipped != (not expected):
        raise ApplicationError("training_skip_invalid", "A selected model branch cannot be skipped.", 409)
    run = db.get(TrainingRun, run_id)
    keys = {"version", "region_id", "run_id", "branch", "bundle_id", "manifest_sha256"}
    if (not isinstance(receipt, dict) or set(receipt) != keys or type(receipt.get("version")) is not int
            or receipt["version"] != 1 or receipt.get("run_id") != str(run_id)
            or receipt.get("region_id") != str(run.region_id) or receipt.get("branch") != branch
            or not isinstance(receipt.get("manifest_sha256"), str)
            or not re.fullmatch("[a-f0-9]{64}", receipt["manifest_sha256"])):
        raise ApplicationError("training_receipt_invalid", "The branch receipt does not match this run.", 409)
    try:
        if str(uuid.UUID(receipt["bundle_id"])) != receipt["bundle_id"]:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ApplicationError("training_receipt_invalid", "Invalid branch bundle identity.", 409) from None
    if current["state"] in {"ready", "skipped"}:
        if current.get("receipt") != receipt:
            raise ApplicationError("training_receipt_conflict", "This branch already accepted a different receipt.", 409)
        db.commit()
        return
    if current["state"] != "running":
        raise ApplicationError("training_branch_closed", "The branch is not accepting output.", 409)
    branches, models = copy.deepcopy(record.branches_json), copy.deepcopy(record.models_json)
    branches[branch].update(state="skipped" if skipped else "ready", receipt=receipt, finished_at=now().isoformat())
    for key in expected:
        models[key].update(state="evaluated", stage="output_verified", evaluation_completed=True)
    record.branches_json, record.models_json = branches, models
    add_event(record, "branch_ready", branch=branch)
    db.commit()


def fail_run(db, run_id, branch, attempt, error_code="training_failed"):
    record = execution(db, run_id, lock=True)
    if record.state in {"completed", "failed"}:
        db.commit()
        return False
    matching_attempt(record, branch, attempt)
    if record.cancel_requested:
        # Preserve the original cause/branch; stop callbacks must not turn a
        # skipped branch into Failed or flood the monitor with duplicate errors.
        db.commit()
        return False
    if error_code not in ERROR_MESSAGES:
        error_code = "training_failed"
    record.cancel_requested = True
    record.state = "cancelling"
    record.error_code = record.error_code or error_code
    record.error_message = record.error_message or ERROR_MESSAGES[error_code]
    branches, models = copy.deepcopy(record.branches_json), copy.deepcopy(record.models_json)
    branches[branch]["state"] = "failed"
    for name, value in models.items():
        if MODEL_BRANCH[name] == branch and value["state"] == "running":
            value.update(state="failed", stage="failed")
        elif value["state"] == "waiting":
            value.update(state="cancelled", stage="not_started")
        elif value["state"] == "running":
            value.update(state="cancelling", stage="stop_requested")
    for value in branches.values():
        if value["state"] == "waiting":
            value.update(state="cancelled", cleanup_confirmed=True)
    record.branches_json, record.models_json = branches, models
    add_event(record, error_code, level="error", branch=branch)
    db.commit()
    return True


def fail_dispatch(db, run_id):
    """Fence a rejected/ambiguous Airflow submission; never leave a queued run.

    If a worker started before the HTTP response was lost, use the same stop
    protocol as other failures. A late DAG cannot claim a closed run.
    """
    record = execution(db, run_id, lock=True)
    if record.state in {"completed", "failed"}:
        db.commit()
        return
    started = [(key, value) for key, value in record.branches_json.items() if value["attempt_id"]]
    if started:
        branch, value = started[0]
        fail_run(db, run_id, branch, value["attempt_id"], "dispatch_failed")
        return
    record.cancel_requested, record.state = True, "failed"
    record.error_code, record.error_message = "dispatch_failed", ERROR_MESSAGES["dispatch_failed"]
    branches, models = copy.deepcopy(record.branches_json), copy.deepcopy(record.models_json)
    for value in branches.values():
        value.update(state="cancelled", cleanup_confirmed=True)
    for value in models.values():
        value.update(state="cancelled", stage="not_started")
    record.branches_json, record.models_json = branches, models
    run = db.get(TrainingRun, run_id)
    run.status, run.completed_at, run.error_message = TrainingRunStatus.FAILED, now(), record.error_message
    add_event(record, "dispatch_failed", level="error")
    db.commit()


def acknowledge_stop(db, run_id, branch, attempt, *, stopped, cleaned):
    if type(stopped) is not bool or type(cleaned) is not bool:
        raise ApplicationError("training_stop_invalid", "Stop and cleanup require explicit boolean confirmation.", 422)
    record = execution(db, run_id, lock=True)
    matching_attempt(record, branch, attempt)
    if not record.cancel_requested:
        raise ApplicationError("training_cancel_not_requested", "No stop was requested for this run.", 409)
    branches = copy.deepcopy(record.branches_json)
    branches[branch].update(stop_confirmed=bool(branches[branch].get("stop_confirmed")) or stopped,
                           cleanup_confirmed=bool(branches[branch].get("cleanup_confirmed")) or cleaned)
    if branches[branch]["stop_confirmed"]:
        branches[branch]["state"] = "cancelled"
        models = copy.deepcopy(record.models_json)
        for name, value in models.items():
            if MODEL_BRANCH[name] == branch and value["state"] in {"running", "cancelling"}:
                value.update(state="cancelled", stage="cancelled")
        record.models_json = models
    record.branches_json = branches
    if all(value.get("cleanup_confirmed") and (value.get("stop_confirmed") or value["attempt_id"] is None)
           for value in branches.values()):
        record.state = "failed"
        run = db.get(TrainingRun, run_id)
        run.status, run.completed_at = TrainingRunStatus.FAILED, now()
        run.error_message = record.error_message
        add_event(record, "run_stopped_and_cleaned", level="error")
    else:
        add_event(record, "stop_pending", level="error", branch=branch,
                  details={"stop_confirmed": bool(stopped), "cleanup_confirmed": bool(cleaned)})
    db.commit()


def publication_gate(db, run_id):
    """Hold this row lock in the registry transaction through its commit."""
    record = execution(db, run_id, lock=True)
    assert_open(record)
    if any(value["state"] not in {"ready", "skipped"} for value in record.branches_json.values()):
        raise ApplicationError("training_branches_incomplete", "All branch receipts must be ready before publication.", 409)
    record.state = "finalizing"
    return record


def complete_in_transaction(record):
    record.state = "completed"
    add_event(record, "run_completed")


def snapshot(record, run, dataset):
    evaluated = sum(bool(value.get("evaluation_completed")) or value["state"] == "evaluated" for value in record.models_json.values())
    return {"training_run_id": str(run.id), "region_id": str(run.region_id), "dataset_id": str(run.dataset_id),
            "dataset": dataset.original_filename,
            "backend": record.backend, "state": record.state, "cancel_requested": record.cancel_requested,
            "error_code": record.error_code, "error_message": record.error_message,
            "models": [{"id": key, **value} for key, value in record.models_json.items()],
            "branches": {branch: {key: value for key, value in item.items() if key != "provider"}
                         for branch, item in record.branches_json.items()},
            "evaluated_models": evaluated, "total_models": len(record.models_json),
            "progress_basis": "models_evaluated", "publication_completed": record.state == "completed",
            "sequence": record.sequence, "events": record.events_json,
            "updated_at": record.updated_at, "started_at": run.started_at,
            "airflow_dag_run_id": run.airflow_dag_run_id}


def visible_runs(db, region_id):
    rows = db.execute(select(TrainingExecution, TrainingRun, Dataset)
                      .join(TrainingRun, TrainingRun.id == TrainingExecution.training_run_id)
                      .join(Dataset, Dataset.id == TrainingRun.dataset_id)
                      .where(TrainingRun.region_id == region_id,
                             TrainingExecution.state != "completed", TrainingExecution.dismissed.is_(False))
                      .order_by(TrainingExecution.created_at.desc()).limit(20)).all()
    return [snapshot(record, run, dataset) for record, run, dataset in rows]


def dismiss_error(db, run_id):
    record = execution(db, run_id, lock=True)
    if record.state != "failed":
        raise ApplicationError("training_error_not_dismissible", "An active or unconfirmed stop cannot be dismissed.", 409)
    record.dismissed = True
    db.commit()

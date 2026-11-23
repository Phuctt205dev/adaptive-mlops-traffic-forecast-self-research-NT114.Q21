"""Remove the stored files of a training run whose models are gone, with a retry journal.

Older model deletions removed only the database record and left the run's files
in object storage and MLflow. This module deletes exactly those files. The
training run record, its history and every other run stay untouched.

It reuses the model-cleanup journal, peer purge and upload tombstones, so a pod
that still holds a local copy can never upload the deleted files again. A run is
refused when any model, task or unfinished deletion could still need its files.
"""
import json
import re
import uuid

from sqlalchemy import select

from backend.app.core.errors import ApplicationError
from backend.app.db.models import AuditLog, ModelVersion, Region, TrainingRun
from backend.app.db.training_execution import TrainingExecution
from backend.app.services.model_cleanup import STEPS, ModelCleanupResources
from backend.app.services.model_cleanup import local_prefixes as model_local_prefixes
from backend.app.services.region_cleanup import (
    MODEL_COMPLETED, MODEL_PENDING, canonical_id, pending_cleanup, progress,
)
from backend.app.services.region_cleanup_resources import fail

TERMINAL_RUN = {"completed", "failed", "cancelled"}
TERMINAL_EXECUTION = {"completed", "failed", "cancelled"}
PENDING_NAMES = ("region_cleanup_pending", "model_cleanup_pending")


def run_locations(region_id, run_id):
    """Every runtime-bucket directory a training run is known to write to."""
    return {
        "serving": f"models/distributed-serving/v1/{region_id}/{run_id}/",
        "exchange": f"training-exchange/v1/regions/{region_id}/runs/{run_id}/",
        "training": f"models/distributed-training/v1/{run_id}/",
        "local_staging": f"local-training/v1/{run_id}/",
        "legacy_run": f"models/regions/{region_id}/training_runs/{run_id}/",
        "legacy_tree_run": f"models/regions/regions/{region_id}/training_runs/{run_id}/",
    }


def removal_blockers(db, run):
    """Reasons this run's files must stay; an empty list means the files are unreferenced."""
    run_id, region_id = str(run.id), str(run.region_id)
    blockers = []
    models = list(db.scalars(select(ModelVersion)))
    if any(str(model.training_run_id) == run_id for model in models):
        blockers.append("a registered model still uses this run")
    recommended = run.recommended_model_version_id
    if recommended and any(model.id == recommended for model in models):
        blockers.append("the run's recommended model still exists")
    if any(run_id in json.dumps([model.artifact_uri, model.mlflow_model_uri,
                                 (db.get(TrainingRun, model.training_run_id).configuration_json
                                  if db.get(TrainingRun, model.training_run_id) else None)],
                                default=str).lower()
           for model in models if str(model.training_run_id) != run_id):
        blockers.append("a live model's files or configuration point at this run")
    status = getattr(run.status, "value", str(run.status))
    if status not in TERMINAL_RUN:
        blockers.append(f"the run is still {status}")
    execution = db.get(TrainingExecution, run.id)
    if execution is not None and execution.state not in TERMINAL_EXECUTION:
        blockers.append("the execution is not finished")
    pending = {str(row.entity_id) for row in db.scalars(select(AuditLog).where(AuditLog.action.in_(PENDING_NAMES)))}
    if region_id in pending or run_id in pending:
        blockers.append("a deletion for this region or run is unfinished")
    if db.get(Region, run.region_id) is None:
        blockers.append("the region record is missing")
    return blockers


def inventory(db, run):
    blockers = removal_blockers(db, run)
    if blockers:
        fail("This run's files are still needed: " + "; ".join(blockers) + ". Nothing was deleted.",
             "run_cleanup_conflict")
    region_id, run_id = str(run.region_id), str(run.id)
    return {"version": 1, "scope": "model", "kind": "training_run_without_model",
            "region_id": region_id, "run_ids": [run_id],
            "local_prefixes": model_local_prefixes(region_id, [run_id])}


class RunDataCleanupResources(ModelCleanupResources):
    def preflight(self, db, plan):
        from mlflow.tracking import MlflowClient
        self.check_idle(plan)
        client = MlflowClient()
        run_id, region_id = plan["run_ids"][0], plan["region_id"]
        live_tracking = {model.mlflow_run_id for model in db.scalars(select(ModelVersion))}
        tracked = {}
        for experiment in client.search_experiments(view_type=3, max_results=1000):
            for key in ("traffic_training_run_id", "training_run_id"):
                entries = client.search_runs([experiment.experiment_id], filter_string=f"tags.{key} = '{run_id}'",
                                             run_view_type=3, max_results=100)
                if len(entries) >= 100:
                    fail("Tracking history exceeds the safe run cleanup budget.")
                for item in entries:
                    info, tags = item.info, item.data.tags
                    owner = {value for name in ("traffic_region_id", "region_id") if (value := tags.get(name))}
                    if (owner - {region_id}
                            or not re.fullmatch("[a-f0-9]{32}", info.run_id)
                            or not str(info.experiment_id).isdigit()
                            or info.experiment_id != experiment.experiment_id
                            or info.artifact_uri != f"mlflow-artifacts:/{info.experiment_id}/{info.run_id}/artifacts"
                            or info.status not in {"FINISHED", "FAILED", "KILLED"}
                            or info.run_id in live_tracking):
                        fail("A tracking run of this training run cannot be proven to belong to it.",
                             "run_cleanup_ownership_conflict")
                    tracked[info.run_id] = info.experiment_id
        plan = {**plan, "mlflow_runs": sorted(tracked), "airflow": [], "objects": [
            *[{"bucket": self.runtime_bucket, "prefix": prefix} for prefix in plan["local_prefixes"]],
            {"bucket": self.runtime_bucket, "prefix": f"training-exchange/v1/regions/{region_id}/runs/{run_id}/"},
            {"bucket": self.runtime_bucket, "prefix": f"local-training/v1/{run_id}/"},
            *[{"bucket": self.mlflow_bucket, "prefix": f"{experiment}/{identity}/"}
              for identity, experiment in sorted(tracked.items())],
        ]}
        for target in plan["objects"]:
            self.objects(target)
            self.multipart_uploads(target)
        return plan


def delete_run_data(db, run_id, *, backend=None):
    backend = backend if backend is not None else RunDataCleanupResources()
    run_id = uuid.UUID(canonical_id(run_id))
    with backend.barrier(db):
        run = db.get(TrainingRun, run_id)
        if run is None:
            raise ApplicationError("run_not_found", "The training run was not found.", 404)
        journal = pending_cleanup(db)
        if journal and (journal.entity_id != run_id or journal.action != MODEL_PENDING):
            fail("Finish the previous deletion before continuing.", "run_cleanup_pending")
        if journal is None:
            if db.scalar(select(AuditLog.id).where(AuditLog.entity_id == run_id, AuditLog.action == MODEL_COMPLETED)):
                return  # A repeated request after completion is safe.
            plan = inventory(db, run)
            try:
                plan = backend.preflight(db, plan)
            except ApplicationError:
                raise
            except Exception:
                raise ApplicationError("run_cleanup_unavailable",
                                       "Could not inspect the run's storage. Nothing was deleted; please try later.",
                                       503) from None
            journal = AuditLog(action=MODEL_PENDING, entity_type="model", entity_id=run_id,
                               after_json={"state": "pending", "phase": "preparing", "plan": plan, "done": []})
            db.add(journal)
            db.commit()
        try:
            plan = journal.after_json["plan"]
            backend.check_idle(plan)
            for step in STEPS:
                if step != "local_files" and step in journal.after_json["done"]:
                    continue
                progress(db, journal, step, state="pending")
                backend.clean(step, plan, str(journal.id))
                progress(db, journal, step, done=list(dict.fromkeys([*journal.after_json["done"], step])))
            progress(db, journal, "verifying")
            backend.verify(plan, str(journal.id))
            journal.action = MODEL_COMPLETED
            progress(db, journal, "completed", state="completed")
        except Exception as error:
            db.rollback()
            journal = db.get(AuditLog, journal.id)
            progress(db, journal, journal.after_json.get("phase", "preparing"),
                     state="retry_required", error_type=type(error).__name__)
            raise ApplicationError("run_cleanup_incomplete",
                                   "Run data deletion is unfinished. Its progress is saved; run it again to continue safely.",
                                   503) from None

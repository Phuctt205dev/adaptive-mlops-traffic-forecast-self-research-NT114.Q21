"""Read-only report: stored files of training runs that no model uses any more.

This tool never deletes, writes or changes anything. It lists, per training run,
what is kept in object storage and MLflow, whether any model still depends on it,
and why a run is (or is not) a safe candidate. A later, separately approved step
decides what to remove.

Run it where the application can reach the database, MinIO and MLflow, for
example inside an API pod:  PYTHONPATH=/app python -B -m scripts.maintenance.unreferenced_run_data
"""
import json
import os
import re
import sys

TERMINAL_RUN = {"completed", "failed", "cancelled"}
TERMINAL_EXECUTION = {"completed", "failed", "cancelled"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MIB = 1024 * 1024
MAX_OBJECTS_PER_PREFIX = 20000


def measure(client, bucket, prefix):
    """Count objects and bytes under one exact directory prefix (listing only)."""
    objects = size = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            if not item["Key"].startswith(prefix):
                raise ValueError("Storage returned a key outside the requested prefix.")
            objects += 1
            size += item.get("Size", 0)
            if objects > MAX_OBJECTS_PER_PREFIX:
                raise ValueError("Too many objects under one prefix for a safe report.")
    return objects, size


def child_directories(client, bucket, prefix):
    names = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for item in page.get("CommonPrefixes", []):
            name = item["Prefix"][len(prefix):].strip("/")
            if UUID.fullmatch(name):
                names.add(name)
    return names


def run_locations(region_id, run_id):
    """Every directory a training run is known to write to (all inside the runtime bucket)."""
    return {
        "serving": f"models/distributed-serving/v1/{region_id}/{run_id}/",
        "exchange": f"training-exchange/v1/regions/{region_id}/runs/{run_id}/",
        "training": f"models/distributed-training/v1/{run_id}/",
        "local_staging": f"local-training/v1/{run_id}/",
        "legacy_run": f"models/regions/{region_id}/training_runs/{run_id}/",
        "legacy_tree_run": f"models/regions/regions/{region_id}/training_runs/{run_id}/",
    }


def mlflow_runs_by_training_run(mlflow_client):
    """MLflow runs grouped by the training run they say they belong to (search only)."""
    grouped, untagged, truncated = {}, [], False
    for experiment in mlflow_client.search_experiments(view_type=3, max_results=1000):
        runs = mlflow_client.search_runs([experiment.experiment_id], run_view_type=3, max_results=1000)
        if len(runs) >= 1000:
            truncated = True
        for run in runs:
            tags = run.data.tags
            owner = tags.get("traffic_training_run_id") or tags.get("training_run_id")
            entry = {"experiment": experiment.experiment_id, "run": run.info.run_id,
                     "experiment_lifecycle": experiment.lifecycle_stage,
                     "run_lifecycle": run.info.lifecycle_stage}
            (grouped.setdefault(owner, []) if owner else untagged).append(entry)
    return grouped, untagged, truncated


def build_report(db, client, mlflow_client, *, runtime_bucket, mlflow_bucket):
    from sqlalchemy import select

    from backend.app.db.models import AuditLog, ModelVersion, Region, TrainingRun
    from backend.app.db.training_execution import TrainingExecution

    models = list(db.scalars(select(ModelVersion)))
    model_runs = {str(model.training_run_id) for model in models}
    # Everything a live model says about where its files are, including the training
    # configuration of its own run. A run id found here is still in use by that model.
    configurations = {str(run.id): run.configuration_json for run in db.scalars(select(TrainingRun))}
    references = [json.dumps([model.artifact_uri, model.mlflow_model_uri,
                              configurations.get(str(model.training_run_id))], default=str).lower()
                  for model in models]
    live_models = {str(model.id) for model in models}
    executions = {str(row.training_run_id): row.state for row in db.scalars(select(TrainingExecution))}
    pending = {str(row.entity_id) for row in db.scalars(select(AuditLog).where(
        AuditLog.action.in_(("region_cleanup_pending", "model_cleanup_pending"))))}
    regions = {str(region.id): region for region in db.scalars(select(Region))}
    runs = list(db.scalars(select(TrainingRun).order_by(TrainingRun.created_at)))
    tracked, untagged, truncated = mlflow_runs_by_training_run(mlflow_client)
    known_runs = {str(run.id) for run in runs}

    report_runs = []
    for run in runs:
        run_id, region_id = str(run.id), str(run.region_id)
        status = getattr(run.status, "value", str(run.status))
        areas = {}
        for name, prefix in run_locations(region_id, run_id).items():
            objects, size = measure(client, runtime_bucket, prefix)
            if objects:
                areas[name] = {"prefix": prefix, "objects": objects, "bytes": size}
        for item in tracked.get(run_id, []):
            objects, size = measure(client, mlflow_bucket, f"{item['experiment']}/{item['run']}/")
            if objects:
                areas.setdefault("mlflow", []).append({**item, "objects": objects, "bytes": size})
        if not areas:
            continue
        blockers = []
        if run_id in model_runs:
            blockers.append("a registered model still uses this run")
        recommended = str(run.recommended_model_version_id) if run.recommended_model_version_id else None
        if recommended and recommended in live_models:
            blockers.append("the run's recommended model still exists")
        if run_id not in model_runs and any(run_id in text for text in references):
            blockers.append("a live model's files or configuration point at this run")
        if status not in TERMINAL_RUN:
            blockers.append(f"the run is still {status}")
        if executions.get(run_id, "completed") not in TERMINAL_EXECUTION:
            blockers.append("the execution is not finished")
        if region_id in pending or run_id in pending:
            blockers.append("a deletion for this region or run is unfinished")
        if region_id not in regions:
            blockers.append("the region record is missing")
        total = sum(a["bytes"] for key, a in areas.items() if key != "mlflow")
        total += sum(m["bytes"] for m in areas.get("mlflow", []))
        report_runs.append({
            "run_id": run_id, "region_id": region_id,
            "region_name": regions[region_id].name if region_id in regions else None,
            "status": status, "created_at": str(run.created_at)[:19], "areas": areas,
            "bytes": total, "removable": not blockers, "blockers": blockers,
        })

    # Folders for run ids the database does not know at all (should normally be none).
    strays = []
    for region_id in regions:
        for prefix_root in (f"models/distributed-serving/v1/{region_id}/",
                            f"training-exchange/v1/regions/{region_id}/runs/"):
            for name in sorted(child_directories(client, runtime_bucket, prefix_root) - known_runs):
                objects, size = measure(client, runtime_bucket, f"{prefix_root}{name}/")
                strays.append({"prefix": f"{prefix_root}{name}/", "objects": objects, "bytes": size})

    # Tracking runs that carry no training-run tag: owner cannot be proven, never a candidate.
    needs_review = []
    for item in untagged:
        objects, size = measure(client, mlflow_bucket, f"{item['experiment']}/{item['run']}/")
        if objects:
            needs_review.append({**item, "objects": objects, "bytes": size,
                                 "reason": "no training-run tag, owner cannot be proven"})

    removable = [item for item in report_runs if item["removable"]]
    return {
        "read_only": True,
        "runs_with_files": len(report_runs),
        "removable_runs": len(removable),
        "removable_bytes": sum(item["bytes"] for item in removable),
        "kept_bytes": sum(item["bytes"] for item in report_runs if not item["removable"]),
        "runs": report_runs,
        "unknown_run_folders": strays,
        "needs_human_review": needs_review,
        "mlflow_history_truncated": truncated,
        "never_candidates": [
            "models of any region and the files they serve from",
            "shared SHAP dependencies",
            "datasets and quality reports",
            "region-level legacy model folders (not per run)",
            "tracking runs without a training-run tag",
        ],
    }


def describe(report):
    lines = [f"READ-ONLY report. Runs holding files: {report['runs_with_files']}; "
             f"removable: {report['removable_runs']} ({report['removable_bytes'] / MIB:.1f} MiB); "
             f"kept: {report['kept_bytes'] / MIB:.1f} MiB."]
    for item in report["runs"]:
        parts = [f"{key}={value['bytes'] / MIB:.1f}" for key, value in item["areas"].items() if key != "mlflow"]
        mlflow = item["areas"].get("mlflow", [])
        if mlflow:
            parts.append(f"mlflow={sum(m['bytes'] for m in mlflow) / MIB:.1f}")
        verdict = "REMOVABLE" if item["removable"] else "KEEP (" + "; ".join(item["blockers"]) + ")"
        lines.append(f"  {item['run_id'][:8]} {item['status']:9} {item['created_at']} "
                     f"{item['bytes'] / MIB:8.1f} MiB  [{', '.join(parts)}]  {verdict}")
    for item in report["needs_human_review"]:
        lines.append(f"  REVIEW {item['experiment']}/{item['run'][:8]} {item['bytes'] / MIB:.1f} MiB - {item['reason']}")
    for item in report["unknown_run_folders"]:
        lines.append(f"  UNKNOWN {item['prefix']} {item['bytes'] / MIB:.1f} MiB")
    return "\n".join(lines)


def main():
    from backend.app.db.session import SessionLocal
    from backend.app.services.storage import get_s3_client
    from mlflow.tracking import MlflowClient

    runtime_bucket = os.environ["TRAFFIC_API_RUNTIME_BUCKET"]
    mlflow_bucket = os.environ["REGION_CLEANUP_MLFLOW_BUCKET"]
    with SessionLocal() as db:
        report = build_report(db, get_s3_client(), MlflowClient(),
                              runtime_bucket=runtime_bucket, mlflow_bucket=mlflow_bucket)
    print(describe(report))
    if "--json" in sys.argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

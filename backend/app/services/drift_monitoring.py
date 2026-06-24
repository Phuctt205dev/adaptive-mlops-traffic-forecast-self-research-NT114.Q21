from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.db.models import (
    Dataset,
    DatasetStatus,
    DriftCheck,
    DriftCheckStatus,
    ModelVersion,
    Region,
    TrainingRun,
    TrainingRunStatus,
)
from backend.app.services import airflow_client, model_registry
from backend.app.services.storage import get_s3_client
from src.feature_drift import calculate_feature_drift


RUNNING_TRAINING_STATUSES = {
    TrainingRunStatus.QUEUED,
    TrainingRunStatus.VALIDATING,
    TrainingRunStatus.PREPROCESSING,
    TrainingRunStatus.TRAINING,
    TrainingRunStatus.EVALUATING,
}


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError("Only s3:// dataset URIs are supported.")
    bucket_key = uri.removeprefix("s3://")
    bucket, key = bucket_key.split("/", 1)
    return bucket, key


def _download_dataset_frame(dataset: Dataset) -> pd.DataFrame:
    bucket, key = _parse_s3_uri(dataset.storage_uri)
    response = get_s3_client().get_object(Bucket=bucket, Key=key)
    frame = pd.read_csv(BytesIO(response["Body"].read()))
    frame["date_time"] = pd.to_datetime(frame["date_time"], errors="raise")
    return frame.sort_values("date_time")


def _current_window_end(current_end_at=None) -> pd.Timestamp:
    timestamp = pd.Timestamp(current_end_at) if current_end_at else pd.Timestamp.now()
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(None)
    return timestamp.floor("h")


def _active_context(db: Session, region: Region) -> tuple[ModelVersion, TrainingRun, Dataset]:
    if region.active_model_version_id is None:
        raise ValueError("Region does not have an active model.")
    model_version = db.get(ModelVersion, region.active_model_version_id)
    if model_version is None:
        raise ValueError("Active model version was not found.")
    training_run = db.get(TrainingRun, model_version.training_run_id)
    if training_run is None:
        raise ValueError("Training run for active model was not found.")
    dataset = db.get(Dataset, training_run.dataset_id)
    if dataset is None or dataset.status != DatasetStatus.VALID:
        raise ValueError("Active model dataset was not found or is not valid.")
    return model_version, training_run, dataset


def _has_running_training(db: Session, region_id: uuid.UUID) -> bool:
    return bool(
        db.scalar(
            select(TrainingRun.id)
            .where(
                TrainingRun.region_id == region_id,
                TrainingRun.status.in_(RUNNING_TRAINING_STATUSES),
            )
            .limit(1)
        )
    )


def _has_recent_retrain(db: Session, region_id: uuid.UUID, cooldown_hours: int) -> bool:
    if cooldown_hours <= 0:
        return False
    cutoff = datetime.now(UTC) - timedelta(hours=cooldown_hours)
    return bool(
        db.scalar(
            select(DriftCheck.id)
            .where(
                DriftCheck.region_id == region_id,
                DriftCheck.triggered_training_run_id.is_not(None),
                DriftCheck.created_at >= cutoff,
            )
            .limit(1)
        )
    )


def _create_check(
    db: Session,
    *,
    region_id: uuid.UUID,
    dataset_id: uuid.UUID | None = None,
    model_version_id: uuid.UUID | None = None,
    reference_start_at=None,
    reference_end_at=None,
    current_start_at=None,
    current_end_at=None,
    status: DriftCheckStatus,
    drift_report: dict | None = None,
    triggered_training_run_id: uuid.UUID | None = None,
    error_message: str | None = None,
) -> DriftCheck:
    summary = (drift_report or {}).get("summary", {})
    drift_check = DriftCheck(
        region_id=region_id,
        dataset_id=dataset_id,
        active_model_version_id=model_version_id,
        reference_start_at=reference_start_at,
        reference_end_at=reference_end_at,
        current_start_at=current_start_at,
        current_end_at=current_end_at,
        status=status,
        drift_detected=bool(summary.get("drift_detected", False)),
        drifted_feature_count=int(summary.get("drifted_feature_count", 0)),
        feature_count=int(summary.get("feature_count", 0)),
        feature_drift_json=drift_report or {},
        triggered_training_run_id=triggered_training_run_id,
        error_message=error_message,
    )
    db.add(drift_check)
    db.commit()
    db.refresh(drift_check)
    return drift_check


def _training_configuration(training_run: TrainingRun, current_end_at: pd.Timestamp) -> dict:
    settings = get_settings()
    previous = training_run.configuration_json or {}
    train_end = pd.Timestamp(current_end_at).normalize()
    train_start = train_end - pd.DateOffset(months=settings.drift_retrain_window_months)
    selected_models = previous.get("selected_models") or [
        "random_forest_lag",
        "xgboost_lag",
        "lightgbm_lag",
        "lstm",
        "gru",
    ]
    return {
        "train_start_date": str(train_start.date()),
        "train_end_date": str(train_end.date()),
        "artifact_root": previous.get("artifact_root", "models/regions"),
        "model_role": "candidate",
        "cv_splits": int(previous.get("cv_splits") or 3),
        "random_state": int(previous.get("random_state") or 42),
        "selected_models": selected_models,
        "recurrent_sequence_length": int(previous.get("recurrent_sequence_length") or 72),
        "recurrent_epochs": int(previous.get("recurrent_epochs") or 3),
        "recurrent_batch_size": int(previous.get("recurrent_batch_size") or 32),
        "final_test_ratio": float(previous.get("final_test_ratio") or 0.15),
        "trigger_source": "feature_drift",
    }


def _trigger_retraining(
    db: Session,
    dataset: Dataset,
    training_run: TrainingRun,
    current_end_at: pd.Timestamp,
) -> TrainingRun:
    settings = get_settings()
    configuration = _training_configuration(training_run, current_end_at)
    dag_run_id = f"drift-training-{dataset.id}-{uuid.uuid4()}"
    queued_run = model_registry.create_queued_training_run(
        db,
        dataset.id,
        dataset.uploaded_by,
        configuration,
        dag_run_id,
    )
    conf = {
        **configuration,
        "region_id": str(dataset.region_id),
        "dataset_id": str(dataset.id),
        "training_run_id": str(queued_run.id),
        "drift_triggered": True,
    }
    try:
        airflow_client.trigger_dag(
            settings.training_dag_id,
            conf,
            dag_run_id=dag_run_id,
        )
    except Exception as error:
        model_registry.mark_training_run_failed(
            db,
            queued_run.id,
            f"Drift retrain trigger failed: {error}",
        )
        raise
    return queued_run


def _check_region(
    db: Session,
    region: Region,
    *,
    auto_retrain: bool,
    force_retrain: bool = False,
    current_end_at=None,
) -> DriftCheck:
    settings = get_settings()
    try:
        model_version, training_run, dataset = _active_context(db, region)
        config = training_run.configuration_json or {}
        train_end_date = config.get("train_end_date")
        if not train_end_date:
            raise ValueError("Active model train_end_date is missing.")

        frame = _download_dataset_frame(dataset)
        train_end = pd.Timestamp(train_end_date)
        reference_end = train_end
        reference_start = reference_end - pd.Timedelta(days=settings.drift_reference_days)
        production = frame[frame["date_time"] >= train_end].copy()
        if production.empty:
            return _create_check(
                db,
                region_id=region.id,
                dataset_id=dataset.id,
                model_version_id=model_version.id,
                status=DriftCheckStatus.SKIPPED,
                error_message="No production data exists after active model train_end_date.",
            )

        current_end = _current_window_end(current_end_at)
        if current_end < train_end:
            return _create_check(
                db,
                region_id=region.id,
                dataset_id=dataset.id,
                model_version_id=model_version.id,
                status=DriftCheckStatus.SKIPPED,
                error_message="Current time is before active model train_end_date.",
            )
        current_start = current_end - pd.Timedelta(days=settings.drift_window_days)
        reference = frame[
            (frame["date_time"] >= reference_start)
            & (frame["date_time"] < reference_end)
        ].copy()
        current = frame[
            (frame["date_time"] > current_start)
            & (frame["date_time"] <= current_end)
        ].copy()

        if len(reference) < settings.drift_min_window_rows:
            return _create_check(
                db,
                region_id=region.id,
                dataset_id=dataset.id,
                model_version_id=model_version.id,
                reference_start_at=reference_start.to_pydatetime(),
                reference_end_at=reference_end.to_pydatetime(),
                current_start_at=current_start.to_pydatetime(),
                current_end_at=current_end.to_pydatetime(),
                status=DriftCheckStatus.SKIPPED,
                error_message="Reference window does not have enough rows.",
            )
        if len(current) < settings.drift_min_window_rows:
            return _create_check(
                db,
                region_id=region.id,
                dataset_id=dataset.id,
                model_version_id=model_version.id,
                reference_start_at=reference_start.to_pydatetime(),
                reference_end_at=reference_end.to_pydatetime(),
                current_start_at=current_start.to_pydatetime(),
                current_end_at=current_end.to_pydatetime(),
                status=DriftCheckStatus.SKIPPED,
                error_message="Current drift window does not have enough rows.",
            )

        drift_report = calculate_feature_drift(
            reference,
            current,
            numeric_threshold=settings.drift_psi_threshold,
            categorical_threshold=settings.drift_js_threshold,
            min_drifted_features=settings.drift_min_drifted_features,
        )
        drift_report["summary"]["reference_row_count"] = len(reference)
        drift_report["summary"]["current_row_count"] = len(current)
        drift_report["summary"]["requested_current_end"] = current_end.isoformat()
        drift_detected = bool(drift_report["summary"]["drift_detected"])
        status = (
            DriftCheckStatus.DRIFT_DETECTED
            if drift_detected
            else DriftCheckStatus.STABLE
        )
        triggered_run = None
        if drift_detected and auto_retrain and settings.drift_auto_retrain:
            if _has_running_training(db, region.id):
                drift_report["retrain_skip_reason"] = "training_already_running"
            elif (
                not force_retrain
                and _has_recent_retrain(db, region.id, settings.drift_retrain_cooldown_hours)
            ):
                drift_report["retrain_skip_reason"] = "cooldown_active"
            else:
                drift_report["force_retrain"] = force_retrain
                triggered_run = _trigger_retraining(
                    db,
                    dataset,
                    training_run,
                    current_end,
                )
                status = DriftCheckStatus.RETRAIN_TRIGGERED

        return _create_check(
            db,
            region_id=region.id,
            dataset_id=dataset.id,
            model_version_id=model_version.id,
            reference_start_at=reference_start.to_pydatetime(),
            reference_end_at=reference_end.to_pydatetime(),
            current_start_at=current_start.to_pydatetime(),
            current_end_at=current_end.to_pydatetime(),
            status=status,
            drift_report=drift_report,
            triggered_training_run_id=triggered_run.id if triggered_run else None,
        )
    except Exception as error:
        return _create_check(
            db,
            region_id=region.id,
            status=DriftCheckStatus.FAILED,
            error_message=str(error),
        )


def _serialize_check(check: DriftCheck) -> dict:
    return {
        "id": str(check.id),
        "region_id": str(check.region_id),
        "dataset_id": str(check.dataset_id) if check.dataset_id else None,
        "active_model_version_id": (
            str(check.active_model_version_id)
            if check.active_model_version_id
            else None
        ),
        "reference_start_at": check.reference_start_at,
        "reference_end_at": check.reference_end_at,
        "current_start_at": check.current_start_at,
        "current_end_at": check.current_end_at,
        "status": check.status.value,
        "drift_detected": check.drift_detected,
        "drifted_feature_count": check.drifted_feature_count,
        "feature_count": check.feature_count,
        "feature_drift_json": check.feature_drift_json,
        "triggered_training_run_id": (
            str(check.triggered_training_run_id)
            if check.triggered_training_run_id
            else None
        ),
        "error_message": check.error_message,
        "created_at": check.created_at,
    }


def list_region_drift_checks(
    db: Session,
    region_id: uuid.UUID,
    *,
    limit: int = 20,
) -> dict:
    region = db.get(Region, region_id)
    if region is None:
        raise ValueError("Region was not found.")
    checks = list(
        db.scalars(
            select(DriftCheck)
            .where(DriftCheck.region_id == region_id)
            .order_by(DriftCheck.created_at.desc())
            .limit(limit)
        )
    )
    latest = checks[0] if checks else None
    return {
        "region_id": str(region_id),
        "latest": _serialize_check(latest) if latest else None,
        "items": [_serialize_check(check) for check in checks],
        "total": len(checks),
    }


def _regions_for_drift_check(db: Session, limit: int) -> list[Region]:
    regions = list(
        db.scalars(
            select(Region).where(
                Region.is_active.is_(True),
                Region.active_model_version_id.is_not(None),
            )
        )
    )
    last_checked = {
        region.id: db.scalar(
            select(func.max(DriftCheck.created_at)).where(
                DriftCheck.region_id == region.id
            )
        )
        for region in regions
    }
    regions.sort(key=lambda region: (last_checked[region.id] is not None, last_checked[region.id] or datetime.min))
    return regions[:limit]


def check_drift_for_region(
    db: Session,
    region_id: uuid.UUID,
    *,
    auto_retrain: bool = True,
    force_retrain: bool = False,
    current_end_at=None,
) -> dict:
    region = db.get(Region, region_id)
    if region is None:
        raise ValueError("Region was not found.")
    check = _check_region(
        db,
        region,
        auto_retrain=auto_retrain,
        force_retrain=force_retrain,
        current_end_at=current_end_at,
    )
    return {
        "checked_regions": 1,
        "drift_detected": 1 if check.drift_detected else 0,
        "retrain_triggered": 1 if check.triggered_training_run_id else 0,
        "checks": [_serialize_check(check)],
    }


def check_drift_for_regions(
    db: Session,
    *,
    auto_retrain: bool = True,
    max_regions: int | None = None,
    current_end_at=None,
) -> dict:
    settings = get_settings()
    limit = max_regions or settings.drift_max_regions_per_run
    regions = _regions_for_drift_check(db, limit)
    checks = [
        _check_region(
            db,
            region,
            auto_retrain=auto_retrain,
            current_end_at=current_end_at,
        )
        for region in regions
    ]
    return {
        "checked_regions": len(checks),
        "drift_detected": sum(1 for check in checks if check.drift_detected),
        "retrain_triggered": sum(1 for check in checks if check.triggered_training_run_id),
        "checks": [_serialize_check(check) for check in checks],
    }

import uuid
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.db.models import (
    Dataset,
    DatasetStatus,
    ModelVersion,
    ModelVersionStatus,
    Prediction,
    Region,
    TrainingRun,
    TrainingRunStatus,
)


SPLIT_POLICY = "time_series_cv_70_15_15"
MODEL_VERSION_PATTERN = re.compile(r"^model_v(?P<number>\d+)$")


def list_region_model_versions(
    db: Session,
    region_id: uuid.UUID,
    page: int,
    page_size: int,
) -> tuple[list[ModelVersion], int]:
    ensure_region_exists(db, region_id)
    query = select(ModelVersion).where(ModelVersion.region_id == region_id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = list(
        db.scalars(
            query.order_by(ModelVersion.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return items, total


def get_model_version_or_404(db: Session, model_version_id: uuid.UUID) -> ModelVersion:
    model_version = db.get(ModelVersion, model_version_id)
    if model_version is None:
        raise ApplicationError(
            "model_version_not_found",
            "Model version was not found.",
            404,
        )
    return model_version


def get_training_run_or_404(db: Session, training_run_id: uuid.UUID) -> TrainingRun:
    training_run = db.get(TrainingRun, training_run_id)
    if training_run is None:
        raise ApplicationError(
            "training_run_not_found",
            "Training run was not found.",
            404,
        )
    return training_run


def activate_model_version(db: Session, model_version_id: uuid.UUID) -> ModelVersion:
    model_version = get_model_version_or_404(db, model_version_id)
    region = ensure_region_exists(db, model_version.region_id)
    training_run = db.get(TrainingRun, model_version.training_run_id)
    configuration = training_run.configuration_json if training_run else {}
    model_family = (configuration or {}).get("model_family")
    inference_supported = (configuration or {}).get("inference_supported", True)
    if not inference_supported or model_family not in {
        None,
        "legacy_sklearn",
        "neural_sequence",
    }:
        raise ApplicationError(
            "model_version_not_activatable",
            "This model version is benchmark-only and cannot be activated for web inference.",
            409,
        )

    previous_active_id = region.active_model_version_id
    if previous_active_id and previous_active_id != model_version.id:
        previous_active = db.get(ModelVersion, previous_active_id)
        if previous_active is not None:
            previous_active.status = ModelVersionStatus.ARCHIVED

    model_version.status = ModelVersionStatus.ACTIVE
    region.active_model_version_id = model_version.id
    db.commit()
    db.refresh(model_version)
    from backend.app.services.predictions import warm_prediction_cache_for_model_best_effort

    warm_prediction_cache_for_model_best_effort(db, model_version)
    return model_version


def delete_model_version(db: Session, model_version_id: uuid.UUID) -> None:
    model_version = get_model_version_or_404(db, model_version_id)
    region = ensure_region_exists(db, model_version.region_id)

    if region.active_model_version_id == model_version.id:
        region.active_model_version_id = None

    db.execute(
        update(TrainingRun)
        .where(TrainingRun.recommended_model_version_id == model_version.id)
        .values(recommended_model_version_id=None)
    )
    db.execute(delete(Prediction).where(Prediction.model_version_id == model_version.id))
    db.delete(model_version)
    db.commit()


def _next_available_model_version(
    db: Session,
    region_id: uuid.UUID,
    desired_version: str,
) -> str:
    existing_versions = set(
        db.scalars(
            select(ModelVersion.version).where(ModelVersion.region_id == region_id)
        )
    )
    if (
        MODEL_VERSION_PATTERN.match(desired_version)
        and desired_version not in existing_versions
    ):
        return desired_version

    numeric_versions = []
    for version in existing_versions:
        match = MODEL_VERSION_PATTERN.match(version)
        if match:
            numeric_versions.append(int(match.group("number")))
    return f"model_v{(max(numeric_versions) if numeric_versions else 0) + 1}"


def _copy_versioned_artifact(model_info: dict, new_version: str) -> None:
    old_version = model_info.get("model_version")
    artifact_path = model_info.get("versioned_model_file")
    if not old_version or old_version == new_version or not artifact_path:
        return

    source = Path(str(artifact_path))
    if not source.exists():
        return
    suffix = source.suffix or ".pkl"
    target = source.with_name(f"{new_version}{suffix}")
    if target != source:
        shutil.copy2(source, target)
        model_info["versioned_model_file"] = str(target)
        if model_info.get("mlflow_model_uri") == str(source):
            model_info["mlflow_model_uri"] = str(target)


def register_training_result(
    db: Session,
    region_id: uuid.UUID,
    dataset_id: uuid.UUID,
    model_info: dict,
    training_run_id: uuid.UUID | None = None,
) -> tuple[TrainingRun, ModelVersion]:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.region_id != region_id:
        raise ApplicationError(
            "dataset_not_found",
            "Dataset was not found for the selected region.",
            404,
        )
    if dataset.status != DatasetStatus.VALID:
        raise ApplicationError(
            "dataset_not_valid",
            "Dataset must be valid before registering a model.",
            400,
        )

    ensure_region_exists(db, region_id)
    now = datetime.now(UTC)
    configuration_json = {
        "train_start_date": model_info.get("train_start_date"),
        "train_end_date": model_info.get("train_end_date"),
        "production_start_at": model_info.get("production_start_at"),
        "production_end_at": model_info.get("production_end_at"),
        "cv_splits": model_info.get("cv_splits"),
        "random_state": model_info.get("random_state"),
        "data_version": model_info.get("data_version"),
    }
    for optional_key in (
        "model_comparison",
        "selected_from_candidates",
        "selected_model_policy",
        "model_family",
        "tree_profile",
        "benchmark_only",
        "inference_supported",
        "preprocessor_file",
        "source_report",
        "split_policy",
        "final_test_used_for_selection",
        "selection_metric",
    ):
        if optional_key in model_info:
            configuration_json[optional_key] = model_info[optional_key]
    if training_run_id:
        training_run = db.get(TrainingRun, training_run_id)
        if training_run is None:
            raise ApplicationError(
                "training_run_not_found",
                "Training run was not found.",
                404,
            )
        if training_run.recommended_model_version_id is not None:
            model_version = db.get(
                ModelVersion,
                training_run.recommended_model_version_id,
            )
            if model_version is not None:
                return training_run, model_version
        training_run.status = TrainingRunStatus.COMPLETED
        training_run.configuration_json = {
            **(training_run.configuration_json or {}),
            **configuration_json,
        }
        training_run.completed_at = now
        if training_run.started_at is None:
            training_run.started_at = now
    else:
        training_run = TrainingRun(
            region_id=region_id,
            dataset_id=dataset_id,
            status=TrainingRunStatus.COMPLETED,
            split_policy=SPLIT_POLICY,
            configuration_json=configuration_json,
            started_at=now,
            completed_at=now,
            requested_by=dataset.uploaded_by,
        )
        db.add(training_run)
    db.flush()

    model_info = dict(model_info)
    model_version = _next_available_model_version(
        db,
        region_id,
        model_info["model_version"],
    )
    _copy_versioned_artifact(model_info, model_version)
    model_info["model_version"] = model_version

    mlflow_run_id = model_info.get("mlflow_run_id") or f"local-{training_run.id}"
    mlflow_model_uri = (
        model_info.get("mlflow_model_uri")
        or model_info.get("versioned_model_file")
        or model_info.get("model_file")
    )
    model_version = ModelVersion(
        region_id=region_id,
        training_run_id=training_run.id,
        variant=model_info["best_model_name"],
        version=model_info["model_version"],
        mlflow_run_id=mlflow_run_id,
        mlflow_model_uri=mlflow_model_uri,
        artifact_uri=model_info.get("versioned_model_file") or model_info["model_file"],
        cv_mean_mae=float(model_info["validation_MAE"]),
        cv_std_mae=float(model_info.get("validation_MAE_std") or 0.0),
        final_test_mae=float(model_info["test_MAE"]),
        status=ModelVersionStatus.CANDIDATE,
    )
    db.add(model_version)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise ApplicationError(
            "model_version_conflict",
            "A model version with this region/version or MLflow run already exists.",
            409,
        ) from error

    training_run.recommended_model_version_id = model_version.id
    db.commit()
    db.refresh(training_run)
    db.refresh(model_version)
    return training_run, model_version


def create_queued_training_run(
    db: Session,
    dataset_id: uuid.UUID,
    requested_by: uuid.UUID,
    configuration: dict,
    dag_run_id: str,
) -> TrainingRun:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise ApplicationError("dataset_not_found", "Dataset was not found.", 404)
    if dataset.status != DatasetStatus.VALID:
        raise ApplicationError(
            "dataset_not_valid",
            "Dataset must be valid before training.",
            400,
        )
    ensure_region_exists(db, dataset.region_id)
    training_run = TrainingRun(
        region_id=dataset.region_id,
        dataset_id=dataset.id,
        airflow_dag_run_id=dag_run_id,
        status=TrainingRunStatus.QUEUED,
        split_policy=SPLIT_POLICY,
        configuration_json=configuration,
        requested_by=requested_by,
    )
    db.add(training_run)
    db.commit()
    db.refresh(training_run)
    return training_run


def mark_training_run_failed(
    db: Session,
    training_run_id: uuid.UUID,
    error_message: str,
) -> TrainingRun:
    training_run = db.get(TrainingRun, training_run_id)
    if training_run is None:
        raise ApplicationError(
            "training_run_not_found",
            "Training run was not found.",
            404,
        )
    training_run.status = TrainingRunStatus.FAILED
    training_run.error_message = error_message
    training_run.completed_at = datetime.now(UTC)
    db.commit()
    db.refresh(training_run)
    return training_run


def ensure_region_exists(db: Session, region_id: uuid.UUID) -> Region:
    region = db.get(Region, region_id)
    if region is None:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    return region

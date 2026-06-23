import uuid
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.errors import ApplicationError
from backend.app.db.models import Dataset, TrainingRun, TrainingRunStatus
from backend.app.db.session import get_db
from backend.app.services.model_registry import (
    mark_training_run_failed,
    register_training_result,
)
from scripts.training.run_region_pipeline import download_dataset
from src.pipeline import run_pipeline
from src.recurrent_region_pipeline import run_recurrent_benchmark


router = APIRouter()


def require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    if x_internal_token != get_settings().internal_training_token:
        raise ApplicationError("internal_token_invalid", "Invalid internal token.", 403)


def _get_training_context(db: Session, training_run_id: uuid.UUID):
    training_run = db.get(TrainingRun, training_run_id)
    if training_run is None:
        raise ApplicationError("training_run_not_found", "Training run was not found.", 404)
    dataset = db.get(Dataset, training_run.dataset_id)
    if dataset is None:
        raise ApplicationError("dataset_not_found", "Dataset was not found.", 404)
    return training_run, dataset


def _mark_training_started(db: Session, training_run: TrainingRun) -> None:
    if training_run.status != TrainingRunStatus.TRAINING:
        training_run.status = TrainingRunStatus.TRAINING
        db.commit()
        db.refresh(training_run)


def _local_dataset_path(training_run: TrainingRun) -> Path:
    return (
        Path("data/region_uploads")
        / str(training_run.region_id)
        / f"{training_run.dataset_id}.csv"
    )


def _ensure_dataset_downloaded(dataset: Dataset, training_run: TrainingRun) -> Path:
    data_path = _local_dataset_path(training_run)
    if not data_path.exists():
        temporary_path = data_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        download_dataset(dataset.storage_uri, temporary_path)
        temporary_path.replace(data_path)
    return data_path


def _branch_results_dir(training_run: TrainingRun) -> Path:
    artifact_root = (training_run.configuration_json or {}).get(
        "artifact_root",
        "models/regions",
    )
    path = (
        Path(artifact_root)
        / str(training_run.region_id)
        / "training_runs"
        / str(training_run.id)
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_branch_result(training_run: TrainingRun, branch_name: str, model_info: dict) -> None:
    path = _branch_results_dir(training_run) / f"{branch_name}.json"
    temporary_path = path.with_suffix(".json.tmp")
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(model_info, file, indent=2, ensure_ascii=False)
    temporary_path.replace(path)


def _read_branch_result(training_run: TrainingRun, branch_name: str) -> dict:
    path = _branch_results_dir(training_run) / f"{branch_name}.json"
    if not path.exists():
        raise ApplicationError(
            "training_branch_result_missing",
            f"Training branch result was not found: {branch_name}.",
            409,
        )
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def _comparison_entry(model_info: dict) -> dict:
    return {
        "model_name": model_info.get("best_model_name"),
        "validation_MAE": model_info.get("validation_MAE"),
        "validation_RMSE": model_info.get("validation_RMSE"),
        "validation_MAPE": model_info.get("validation_MAPE"),
        "test_MAE": model_info.get("test_MAE"),
        "test_RMSE": model_info.get("test_RMSE"),
        "test_MAPE": model_info.get("test_MAPE"),
        "benchmark_only": bool(model_info.get("benchmark_only")),
        "inference_supported": bool(model_info.get("inference_supported", True)),
        "mlflow_run_id": model_info.get("mlflow_run_id"),
        "artifact_uri": model_info.get("versioned_model_file") or model_info.get("model_file"),
    }


@router.post("/training-runs/{training_run_id}/execute/tree")
def execute_tree_training_branch(
    training_run_id: uuid.UUID,
    _token: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
):
    training_run, dataset = _get_training_context(db, training_run_id)
    _mark_training_started(db, training_run)

    config = training_run.configuration_json or {}

    try:
        data_path = _ensure_dataset_downloaded(dataset, training_run)
        model_dir = (
            Path(config.get("artifact_root", "models/regions"))
            / str(training_run.region_id)
            / "models"
        )
        model_role = config.get("model_role", "candidate")
        model_info = run_pipeline(
            train_start_date=config["train_start_date"],
            train_end_date=config["train_end_date"],
            output_model_path=str(model_dir / f"{model_role}_model.pkl"),
            output_info_path=str(model_dir / f"{model_role}_model_info.json"),
            model_role=model_role,
            random_state=int(config.get("random_state", 42)),
            cv_splits=int(config.get("cv_splits", 3)),
            data_path=str(data_path),
            region_id=training_run.region_id,
            dataset_id=training_run.dataset_id,
            artifact_root=config.get("artifact_root", "models/regions"),
        )
        _write_branch_result(training_run, "tree", model_info)
        return {
            "training_run_id": str(training_run.id),
            "branch": "tree",
            "best_model_name": model_info["best_model_name"],
            "validation_MAE": model_info["validation_MAE"],
            "test_MAE": model_info["test_MAE"],
        }
    except Exception as error:
        mark_training_run_failed(db, training_run.id, str(error))
        raise


@router.post("/training-runs/{training_run_id}/execute/recurrent/{model_name}")
def execute_recurrent_training_branch(
    training_run_id: uuid.UUID,
    model_name: str,
    _token: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
):
    normalized_model_name = model_name.upper()
    if normalized_model_name not in {"LSTM", "GRU"}:
        raise ApplicationError(
            "recurrent_model_not_supported",
            "Only LSTM and GRU recurrent models are supported.",
            400,
        )

    training_run, dataset = _get_training_context(db, training_run_id)
    _mark_training_started(db, training_run)
    config = training_run.configuration_json or {}

    try:
        data_path = _ensure_dataset_downloaded(dataset, training_run)
        model_info = run_recurrent_benchmark(
            model_name=normalized_model_name,
            train_start_date=config["train_start_date"],
            train_end_date=config["train_end_date"],
            model_role=config.get("model_role", "candidate"),
            random_state=int(config.get("random_state", 42)),
            data_path=str(data_path),
            region_id=training_run.region_id,
            dataset_id=training_run.dataset_id,
            artifact_root=config.get("artifact_root", "models/regions"),
            sequence_length=int(config.get("recurrent_sequence_length", 24)),
            epochs=int(config.get("recurrent_epochs", 8)),
            batch_size=int(config.get("recurrent_batch_size", 128)),
        )
        _write_branch_result(
            training_run,
            normalized_model_name.lower(),
            model_info,
        )
        return {
            "training_run_id": str(training_run.id),
            "branch": normalized_model_name.lower(),
            "best_model_name": model_info["best_model_name"],
            "validation_MAE": model_info["validation_MAE"],
            "test_MAE": model_info["test_MAE"],
        }
    except Exception as error:
        mark_training_run_failed(db, training_run.id, str(error))
        raise


@router.post("/training-runs/{training_run_id}/finalize")
def finalize_parallel_training_run(
    training_run_id: uuid.UUID,
    _token: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
):
    training_run, _dataset = _get_training_context(db, training_run_id)

    try:
        tree_info = _read_branch_result(training_run, "tree")
        branch_infos = [
            tree_info,
            _read_branch_result(training_run, "lstm"),
            _read_branch_result(training_run, "gru"),
        ]

        model_comparison = [_comparison_entry(item) for item in branch_infos]
        best_benchmark = min(
            model_comparison,
            key=lambda item: float(item["validation_MAE"]),
        )
        tree_info["model_comparison"] = model_comparison
        tree_info["best_benchmark_model"] = best_benchmark["model_name"]
        tree_info["selected_model_policy"] = "best_inference_supported_model"
        tree_info["neural_models_benchmark_only"] = True

        completed_run, model_version = register_training_result(
            db,
            training_run.region_id,
            training_run.dataset_id,
            tree_info,
            training_run_id=training_run.id,
        )
        return {
            "training_run_id": str(completed_run.id),
            "training_run_status": completed_run.status.value,
            "model_version_id": str(model_version.id),
            "model_version_status": model_version.status.value,
        }
    except Exception as error:
        mark_training_run_failed(db, training_run.id, str(error))
        raise


@router.post("/training-runs/{training_run_id}/execute")
def execute_training_run(
    training_run_id: uuid.UUID,
    _token: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
):
    tree_response = execute_tree_training_branch(training_run_id, _token, db)
    # Backward-compatible endpoint for manual callers. It trains and registers
    # the tree model only; Airflow uses the parallel branch endpoints above.
    training_run, _dataset = _get_training_context(db, training_run_id)
    tree_info = _read_branch_result(training_run, "tree")
    completed_run, model_version = register_training_result(
        db,
        training_run.region_id,
        training_run.dataset_id,
        tree_info,
        training_run_id=training_run.id,
    )
    return {
        **tree_response,
        "training_run_status": completed_run.status.value,
        "model_version_id": str(model_version.id),
        "model_version_status": model_version.status.value,
    }

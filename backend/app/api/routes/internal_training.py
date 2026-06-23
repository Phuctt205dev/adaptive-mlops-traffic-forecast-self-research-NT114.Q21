import uuid
import json
import threading
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
from src.region_training_variants import (
    normalize_selected_models,
    select_best_candidate,
    train_region_recurrent_candidate,
    train_region_tree_lag_candidates,
)


router = APIRouter()
_RECURRENT_TRAINING_LOCK = threading.Lock()


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
        "model_family": model_info.get("model_family"),
        "validation_MAE": model_info.get("validation_MAE"),
        "validation_MAE_std": model_info.get("validation_MAE_std"),
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
        branch_result = train_region_tree_lag_candidates(
            selected_models=config.get("selected_models"),
            data_path=str(data_path),
            artifact_root=config.get("artifact_root", "models/regions"),
            region_id=training_run.region_id,
            dataset_id=training_run.dataset_id,
            training_run_id=training_run.id,
            config=config,
        )
        _write_branch_result(training_run, "tree", branch_result)
        return {
            "training_run_id": str(training_run.id),
            "branch": "tree",
            "status": branch_result["status"],
            "trained_models": [
                item["best_model_name"]
                for item in branch_result.get("candidates", [])
            ],
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
        with _RECURRENT_TRAINING_LOCK:
            branch_result = train_region_recurrent_candidate(
                selected_models=config.get("selected_models"),
                model_name=normalized_model_name,
                data_path=str(data_path),
                artifact_root=config.get("artifact_root", "models/regions"),
                region_id=training_run.region_id,
                dataset_id=training_run.dataset_id,
                training_run_id=training_run.id,
                config=config,
            )
        _write_branch_result(
            training_run,
            normalized_model_name.lower(),
            branch_result,
        )
        return {
            "training_run_id": str(training_run.id),
            "branch": normalized_model_name.lower(),
            "status": branch_result["status"],
            "trained_models": [
                item["best_model_name"]
                for item in branch_result.get("candidates", [])
            ],
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
        config = training_run.configuration_json or {}
        selected_models = normalize_selected_models(config.get("selected_models"))
        tree_info = _read_branch_result(training_run, "tree")
        branch_infos = [
            tree_info,
            _read_branch_result(training_run, "lstm"),
            _read_branch_result(training_run, "gru"),
        ]

        candidates = [
            candidate
            for branch in branch_infos
            for candidate in branch.get("candidates", [])
        ]
        trained_model_names = {item["best_model_name"] for item in candidates}
        missing = sorted(
            set(selected_models) - trained_model_names
        )
        if missing:
            raise ApplicationError(
                "training_selected_models_missing",
                f"Selected models were not trained: {', '.join(missing)}.",
                409,
            )

        best_info = select_best_candidate(candidates)
        model_comparison = [_comparison_entry(item) for item in candidates]
        best_info["model_comparison"] = model_comparison
        best_info["selected_from_candidates"] = selected_models
        best_info["selected_model_policy"] = (
            "lowest_cross_validation_mean_MAE_then_CV_std"
        )

        completed_run, model_version = register_training_result(
            db,
            training_run.region_id,
            training_run.dataset_id,
            best_info,
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
    # the best selected tree model only; Airflow uses the parallel branch endpoints above.
    training_run, _dataset = _get_training_context(db, training_run_id)
    tree_branch = _read_branch_result(training_run, "tree")
    tree_info = select_best_candidate(tree_branch.get("candidates", []))
    tree_info["model_comparison"] = [
        _comparison_entry(item)
        for item in tree_branch.get("candidates", [])
    ]
    tree_info["selected_model_policy"] = (
        "lowest_cross_validation_mean_MAE_then_CV_std_tree_only"
    )
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

"""Worker storage/finalization contract; no model construction or fitting."""
import uuid,json,os
from pathlib import Path
from urllib.parse import urlparse
from fastapi import Depends,Header
from sqlalchemy.orm import Session
from backend.app.core.config import get_settings
from backend.app.core.errors import ApplicationError
from backend.app.db.session import get_db
from backend.app.db.models import Dataset,Region,TrainingRun,TrainingRunStatus
from backend.app.services.model_registry import mark_training_run_failed,register_training_result
from src.model_selection import select_best_candidate
from src.model_variants import normalize_selected_models

def download_dataset(storage_uri, destination):
    from backend.app.services.storage import get_s3_client
    parsed=urlparse(storage_uri)
    if parsed.scheme!='s3' or not parsed.netloc or not parsed.path:
        raise ApplicationError('training_dataset_invalid','Dataset storage reference is invalid.',422)
    destination.parent.mkdir(parents=True,exist_ok=True)
    get_s3_client().download_file(parsed.netloc,parsed.path.lstrip('/'),str(destination))
    return destination

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
    region = db.get(Region, training_run.region_id)
    if region is None:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    return training_run, dataset, region


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


def _read_optional_branch_result(training_run: TrainingRun, branch_name: str) -> dict:
    path = _branch_results_dir(training_run) / f"{branch_name}.json"
    if not path.exists():
        return {
            "branch": branch_name,
            "status": "skipped",
            "candidates": [],
        }
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def _comparison_entry(model_info: dict) -> dict:
    return {
        "model_name": model_info.get("model_name") or model_info.get("best_model_name"),
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
        "selected": bool(model_info.get("selected", False)),
    }


def _comparison_entries(model_info: dict) -> list[dict]:
    nested = model_info.get("model_comparison") or []
    if not nested:
        return [_comparison_entry(model_info)]
    entries = []
    for item in nested:
        entry = _comparison_entry(
            {
                **item,
                "model_family": item.get("model_family") or model_info.get("model_family"),
            }
        )
        entries.append(entry)
    return entries


def _metric_entry(model_info: dict) -> dict:
    return {
        "best_model_name": model_info.get("best_model_name"),
        "model_family": model_info.get("model_family"),
        "validation_MAE": model_info.get("validation_MAE"),
        "validation_MAE_std": model_info.get("validation_MAE_std"),
        "test_MAE": model_info.get("test_MAE"),
        "test_RMSE": model_info.get("test_RMSE"),
        "test_MAPE": model_info.get("test_MAPE"),
        "model_version": model_info.get("model_version"),
        "benchmark_only": bool(model_info.get("benchmark_only")),
        "inference_supported": bool(model_info.get("inference_supported", True)),
    }


def _branch_xcom_response(training_run: TrainingRun, branch_result: dict) -> dict:
    candidates = branch_result.get("candidates", [])
    best = None
    if candidates:
        best = branch_result.get("production_candidate") or select_best_candidate(candidates)
    response = {
        "training_run_id": str(training_run.id),
        "branch": branch_result.get("branch"),
        "status": branch_result.get("status"),
        "trained_models": [
            item["best_model_name"]
            for item in candidates
        ],
        "candidates": [_metric_entry(item) for item in candidates],
    }
    if best:
        response.update(_metric_entry(best))
    return response


def finalize_parallel_training_run(
    training_run_id: uuid.UUID,
    _token: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
):
    training_run, _dataset, _region = _get_training_context(db, training_run_id)

    try:
        config = training_run.configuration_json or {}
        selected_models = normalize_selected_models(config.get("selected_models"))
        tree_info = _read_branch_result(training_run, "tree")
        branch_infos = [
            tree_info,
            _read_optional_branch_result(training_run, "lstm"),
            _read_optional_branch_result(training_run, "gru"),
        ]

        candidates = [
            candidate
            for branch in branch_infos
            for candidate in branch.get("candidates", [])
        ]
        selected_recurrent = {
            model for model in selected_models if model in {"lstm", "gru"}
        }
        trained_recurrent = {
            item["best_model_name"].lower()
            for item in candidates
            if item.get("model_family") == "neural_sequence"
        }
        missing_recurrent = sorted(
            selected_recurrent - trained_recurrent
        )
        if missing_recurrent:
            raise ApplicationError(
                "training_selected_models_missing",
                f"Selected recurrent models were not trained: {', '.join(missing_recurrent)}.",
                409,
            )

        production_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("inference_supported", True)
            and not candidate.get("benchmark_only", False)
        ]
        best_info = select_best_candidate(production_candidates)
        if best_info is None:
            raise ApplicationError(
                "production_model_missing",
                "Training did not produce a production-compatible model.",
                409,
            )
        model_comparison = [
            entry
            for candidate in candidates
            for entry in _comparison_entries(candidate)
        ]
        for entry in model_comparison:
            entry["selected"] = bool(entry.get("selected")) or (
                entry.get("model_name") == best_info.get("best_model_name")
                and entry.get("model_family") == best_info.get("model_family")
            )
        best_info["model_comparison"] = model_comparison
        best_info["selected_from_candidates"] = selected_models
        best_info["feature_profiles"] = {key: profile for candidate in production_candidates
                                         for key, profile in candidate.get("feature_profiles", {}).items()}
        best_info["shap_reports"] = {name: metadata for candidate in production_candidates
                                    for name, metadata in candidate.get("shap_reports", {}).items()}
        best_info["selected_model_policy"] = (
            "lowest_cross_validation_mean_MAE_then_CV_std_among_inference_supported_models"
        )
        best_info["benchmark_only"] = False
        best_info["inference_supported"] = True

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
            "model_version": model_version.version,
            "model_variant": model_version.variant,
            "model_version_status": model_version.status.value,
            "best_model_name": best_info.get("best_model_name"),
            "validation_MAE": best_info.get("validation_MAE"),
            "validation_MAE_std": best_info.get("validation_MAE_std"),
            "test_MAE": best_info.get("test_MAE"),
            "candidates": model_comparison,
        }
    except Exception as error:
        mark_training_run_failed(db, training_run.id, str(error))
        raise

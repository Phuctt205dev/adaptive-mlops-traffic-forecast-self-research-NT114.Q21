from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from src.time_series_cross_validation import run_model_time_series_cross_validation
from src.time_series_features import create_time_series_features, save_time_series_features
from src.mlflow_regions import region_experiment_name
from src.time_series_preprocess import prepare_hourly_time_series, save_hourly_dataset


TREE_LAG_VARIANTS = {
    "random_forest_lag": "RandomForest",
    "xgboost_lag": "XGBoost",
    "lightgbm_lag": "LightGBM",
}
RECURRENT_VARIANTS = {
    "lstm": "LSTM",
    "gru": "GRU",
}
PRODUCTION_MODEL_VARIANTS = (
    *TREE_LAG_VARIANTS.keys(),
    *RECURRENT_VARIANTS.keys(),
)
DEFAULT_PRODUCTION_MODEL_VARIANTS = tuple(PRODUCTION_MODEL_VARIANTS)


def normalize_selected_models(selected_models):
    if not selected_models:
        return list(DEFAULT_PRODUCTION_MODEL_VARIANTS)
    normalized = []
    for model in selected_models:
        value = str(model).strip().lower()
        if value not in PRODUCTION_MODEL_VARIANTS:
            raise ValueError(f"Unsupported training model variant: {model}.")
        if value not in normalized:
            normalized.append(value)
    return normalized


def selected_tree_variants(selected_models):
    return [
        variant
        for variant in normalize_selected_models(selected_models)
        if variant in TREE_LAG_VARIANTS
    ]


def selected_recurrent_variant(selected_models, model_name):
    variant = str(model_name).strip().lower()
    if variant in {"lstm", "gru"}:
        return variant if variant in normalize_selected_models(selected_models) else None
    upper_name = str(model_name).strip().upper()
    for candidate, supported_name in RECURRENT_VARIANTS.items():
        if upper_name == supported_name:
            return candidate if candidate in normalize_selected_models(selected_models) else None
    raise ValueError(f"Unsupported recurrent model: {model_name}.")


def _run_root(artifact_root, region_id, training_run_id):
    return Path(artifact_root) / str(region_id) / "training_runs" / str(training_run_id)


def _models_dir(artifact_root, region_id):
    path = Path(artifact_root) / str(region_id) / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prepare_region_time_series_inputs(
    data_path,
    artifact_root,
    region_id,
    training_run_id,
    branch_name,
):
    raw_df = pd.read_csv(data_path)
    hourly_df, audit_df, hourly_report = prepare_hourly_time_series(raw_df)
    feature_df, feature_report = create_time_series_features(hourly_df, audit_df)

    input_dir = (
        _run_root(artifact_root, region_id, training_run_id)
        / "prepared"
        / branch_name
    )
    hourly_path = input_dir / "hourly.csv"
    audit_path = input_dir / "hourly_audit.csv"
    hourly_report_path = input_dir / "hourly_report.json"
    feature_path = input_dir / "features.csv"
    feature_report_path = input_dir / "feature_report.json"
    save_hourly_dataset(
        hourly_df,
        audit_df,
        hourly_report,
        str(hourly_path),
        str(audit_path),
        str(hourly_report_path),
    )
    save_time_series_features(
        feature_df,
        feature_report,
        str(feature_path),
        str(feature_report_path),
    )
    return {
        "hourly_df": hourly_df,
        "audit_df": audit_df,
        "feature_df": feature_df,
        "hourly_path": str(hourly_path),
        "audit_path": str(audit_path),
        "feature_path": str(feature_path),
        "hourly_report_path": str(hourly_report_path),
        "feature_report_path": str(feature_report_path),
    }


def _candidate_from_report(
    report,
    region_id,
    dataset_id,
    data_path,
    model_role,
):
    saved_at = datetime.now().isoformat()
    variant = report["variant"]
    display_name = report.get("model", variant)
    return {
        "best_model_name": display_name,
        "model_version": f"{variant}_{saved_at.replace(':', '').replace('-', '')}",
        "model_role": model_role,
        "model_family": report["family"],
        "tree_profile": report.get("tree_profile"),
        "benchmark_only": False,
        "inference_supported": True,
        "region_id": str(region_id),
        "dataset_id": str(dataset_id),
        "data_path": data_path,
        "train_start_date": report["split_policy"].get("train_start_date"),
        "train_end_date": report["split_policy"].get("train_end_date"),
        "production_start_at": report["production_reserved"].get("start"),
        "production_end_at": report["production_reserved"].get("end"),
        "validation_MAE": report["cv_metrics"]["MAE"]["mean"],
        "validation_MAE_std": report["cv_metrics"]["MAE"]["std"],
        "validation_RMSE": report["cv_metrics"]["RMSE"]["mean"],
        "validation_MAPE": report["cv_metrics"]["MAPE"]["mean"],
        "cv_splits": report["split_policy"]["cv_splits"],
        "test_MAE": report["final_test_metrics"]["MAE"],
        "test_RMSE": report["final_test_metrics"]["RMSE"],
        "test_MAPE": report["final_test_metrics"]["MAPE"],
        "random_state": report["random_state"],
        "saved_at": saved_at,
        "model_file": report["model_path"],
        "versioned_model_file": report["model_path"],
        "mlflow_run_id": report.get("mlflow_run_id"),
        "mlflow_model_uri": report.get("mlflow_model_uri"),
        "preprocessor_file": report.get("preprocessor_path"),
        "artifact_paths": report["artifact_paths"],
        "source_report": report["report_path"],
        "split_policy": report["split_policy"],
        "final_test_used_for_selection": False,
        "selection_metric": "cross_validation_mean_MAE",
    }


def _train_variant(
    variant,
    prepared,
    artifact_root,
    region_id,
    dataset_id,
    training_run_id,
    config,
    data_path,
    region_name=None,
):
    model_name = TREE_LAG_VARIANTS.get(variant) or RECURRENT_VARIANTS[variant]
    run_root = _run_root(artifact_root, region_id, training_run_id)
    output_directory = _models_dir(artifact_root, region_id) / "training_runs" / str(training_run_id)
    result_directory = run_root / "results"

    report = run_model_time_series_cross_validation(
        model_name=model_name,
        feature_df=prepared["feature_df"],
        hourly_df=prepared["hourly_df"],
        audit_df=prepared["audit_df"],
        n_splits=int(config.get("cv_splits", 3)),
        sequence_length=int(config.get("recurrent_sequence_length", 72)),
        max_epochs=int(config.get("recurrent_epochs", config.get("max_epochs", 3))),
        batch_size=int(config.get("recurrent_batch_size", config.get("batch_size", 32))),
        random_state=int(config.get("random_state", 42)),
        output_directory=str(output_directory),
        result_directory=str(result_directory),
        verbose=int(config.get("neural_verbose", 0)),
        tree_profile="autoregressive",
        artifact_name=variant,
        train_start_date=config["train_start_date"],
        train_end_date=config["train_end_date"],
        final_test_ratio=float(config.get("final_test_ratio", 0.15)),
        experiment_name=region_experiment_name(region_name, region_id),
        mlflow_dataset_name=f"region_{region_id}_dataset_{dataset_id}",
        mlflow_dataset_source=str(data_path),
    )
    return _candidate_from_report(
        report,
        region_id=region_id,
        dataset_id=dataset_id,
        data_path=data_path,
        model_role=config.get("model_role", "candidate"),
    )


def train_region_tree_lag_candidates(
    selected_models,
    data_path,
    artifact_root,
    region_id,
    dataset_id,
    training_run_id,
    config,
    region_name=None,
):
    variants = selected_tree_variants(selected_models)
    if not variants:
        return {"branch": "tree", "status": "skipped", "candidates": []}
    prepared = _prepare_region_time_series_inputs(
        data_path,
        artifact_root,
        region_id,
        training_run_id,
        "tree",
    )
    candidates = [
        _train_variant(
            variant,
            prepared,
            artifact_root,
            region_id,
            dataset_id,
            training_run_id,
            config,
            data_path,
            region_name=region_name,
        )
        for variant in variants
    ]
    return {
        "branch": "tree",
        "status": "completed",
        "prepared_paths": {
            key: value
            for key, value in prepared.items()
            if key.endswith("_path")
        },
        "candidates": candidates,
    }


def train_region_recurrent_candidate(
    selected_models,
    model_name,
    data_path,
    artifact_root,
    region_id,
    dataset_id,
    training_run_id,
    config,
    region_name=None,
):
    variant = selected_recurrent_variant(selected_models, model_name)
    if variant is None:
        return {
            "branch": str(model_name).lower(),
            "status": "skipped",
            "candidates": [],
        }
    prepared = _prepare_region_time_series_inputs(
        data_path,
        artifact_root,
        region_id,
        training_run_id,
        variant,
    )
    return {
        "branch": variant,
        "status": "completed",
        "candidates": [
            _train_variant(
                variant,
                prepared,
                artifact_root,
                region_id,
                dataset_id,
                training_run_id,
                config,
                data_path,
                region_name=region_name,
            )
        ],
    }


def select_best_candidate(candidates):
    if not candidates:
        raise ValueError("No model candidates were trained.")
    return min(
        candidates,
        key=lambda item: (
            float(item["validation_MAE"]),
            float(item.get("validation_MAE_std") or 0.0),
            str(item["best_model_name"]),
        ),
    )

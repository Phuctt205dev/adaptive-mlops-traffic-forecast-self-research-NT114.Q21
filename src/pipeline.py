import json
import os
from contextlib import nullcontext
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (
    mean_absolute_error,
    mean_absolute_percentage_error,
    mean_squared_error,
)
from sklearn.model_selection import TimeSeriesSplit

from src.preprocess import (
    load_observed_target_data,
    preprocess,
    split_data,
)

class _NullMLflowData:
    @staticmethod
    def from_pandas(*_args, **_kwargs):
        return None


class _NullMLflowSklearn:
    @staticmethod
    def log_model(*_args, **_kwargs):
        return None


class _NullMLflow:
    data = _NullMLflowData()
    sklearn = _NullMLflowSklearn()

    @staticmethod
    def set_tracking_uri(*_args, **_kwargs):
        return None

    @staticmethod
    def set_experiment(*_args, **_kwargs):
        return None

    @staticmethod
    def start_run(*_args, **_kwargs):
        return nullcontext()

    @staticmethod
    def log_input(*_args, **_kwargs):
        return None

    @staticmethod
    def log_metrics(*_args, **_kwargs):
        return None

    @staticmethod
    def log_params(*_args, **_kwargs):
        return None

    @staticmethod
    def set_tags(*_args, **_kwargs):
        return None

    @staticmethod
    def log_artifact(*_args, **_kwargs):
        return None


try:
    import mlflow
    import mlflow.data
    import mlflow.sklearn
except ModuleNotFoundError:
    mlflow = _NullMLflow()


def train_random_forest(*args, **kwargs):
    from src.train import train_random_forest as trainer

    return trainer(*args, **kwargs)


def train_xgboost(*args, **kwargs):
    from src.train import train_xgboost as trainer

    return trainer(*args, **kwargs)


def train_lightgbm(*args, **kwargs):
    from src.train import train_lightgbm as trainer

    return trainer(*args, **kwargs)


DATA_PATH = "data/TrafficVolumeData.csv"
DEFAULT_RANDOM_STATE = 42
DEFAULT_CV_SPLITS = 3
DEFAULT_MLFLOW_TRACKING_URI = "file:./mlruns"

os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")


def evaluate(y_true, y_pred):
    """Return the metrics used to compare regression models."""
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    mape = mean_absolute_percentage_error(y_true, y_pred) * 100
    return mae, rmse, mape


def _get_run_id(run):
    return getattr(getattr(run, "info", None), "run_id", None)


def _append_csv_row(path, row, columns):
    """Append one structured row while remaining compatible with old CSV files."""
    if os.path.exists(path):
        dataframe = pd.read_csv(path)
    else:
        dataframe = pd.DataFrame(columns=columns)

    dataframe = pd.concat(
        [dataframe, pd.DataFrame([row])],
        ignore_index=True,
    )
    dataframe.to_csv(path, index=False)


def _region_base_path(artifact_root, region_id):
    if region_id is None:
        return artifact_root
    return os.path.join(artifact_root, "regions", str(region_id))


def _default_model_path(artifact_root, region_id, filename):
    base_path = _region_base_path(artifact_root, region_id)
    return os.path.join(base_path, "models", filename)


def _default_data_versions_dir(artifact_root, region_id):
    base_path = _region_base_path(artifact_root, region_id)
    return os.path.join(base_path, "data_versions")


def create_data_version(
    df,
    train_start_date,
    train_end_date,
    data_versions_dir="data_versions",
    metadata=None,
):
    os.makedirs(data_versions_dir, exist_ok=True)
    log_path = os.path.join(data_versions_dir, "version_log.csv")

    if os.path.exists(log_path):
        version_number = len(pd.read_csv(log_path)) + 1
    else:
        version_number = 1

    version_name = f"data_v{version_number}"
    version_file = os.path.join(data_versions_dir, f"{version_name}.csv")
    df.to_csv(version_file, index=False)

    row = {
        "version": version_name,
        "train_start": train_start_date,
        "train_end": train_end_date,
        "rows": len(df),
    }
    if metadata:
        row.update(metadata)

    _append_csv_row(
        log_path,
        row,
        list(row.keys()),
    )

    print(f"\nData version created: {version_name}")
    return version_name


def create_model_version(models_dir="models", metadata=None):
    if not models_dir:
        models_dir = "."
    os.makedirs(models_dir, exist_ok=True)
    version_file = os.path.join(models_dir, "model_versions.csv")

    if os.path.exists(version_file):
        version_number = len(pd.read_csv(version_file)) + 1
    else:
        version_number = 1

    model_version = f"model_v{version_number}"
    row = {
        "version": model_version,
        "created_at": datetime.now().isoformat(),
    }
    if metadata:
        row.update(metadata)

    _append_csv_row(
        version_file,
        row,
        list(row.keys()),
    )

    print(f"\nModel version created: {model_version}")
    return model_version


def save_model_info(info, output_path):
    output_directory = os.path.dirname(output_path)
    if output_directory:
        os.makedirs(output_directory, exist_ok=True)

    temporary_path = f"{output_path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(info, file, indent=4, ensure_ascii=False)
    os.replace(temporary_path, output_path)


def _validate_training_data(df):
    if df.empty:
        raise ValueError("No training data exists in the requested date range.")

    # Each split must contain data before model training can start.
    if len(df) < 7:
        raise ValueError("Training data is too small for train/validation/test split.")


def run_pipeline(
    train_start_date,
    train_end_date,
    output_model_path=None,
    output_info_path=None,
    model_role="champion",
    random_state=DEFAULT_RANDOM_STATE,
    cv_splits=DEFAULT_CV_SPLITS,
    data_path=DATA_PATH,
    data_audit_path=None,
    region_id=None,
    dataset_id=None,
    artifact_root=".",
    mlflow_tracking_uri=None,
    experiment_name=None,
):
    """
    Train all supported models and save the best validation-MAE model.

    Retraining workers should use model_role="candidate" and candidate output
    paths. Only the promotion step is allowed to replace the champion model.
    """
    print(f"\nTraining {model_role} model...")
    print(f"Random state: {random_state}")
    print(f"Data path: {data_path}")
    if region_id is not None:
        print(f"Region ID: {region_id}")
    if dataset_id is not None:
        print(f"Dataset ID: {dataset_id}")

    models_dir = os.path.dirname(
        output_model_path
        or _default_model_path(artifact_root, region_id, "best_model.pkl")
    )
    data_versions_dir = _default_data_versions_dir(artifact_root, region_id)
    if output_model_path is None:
        output_model_path = os.path.join(models_dir, "best_model.pkl")
    if output_info_path is None:
        output_info_path = os.path.join(models_dir, "best_model_info.json")

    mlflow.set_tracking_uri(
        mlflow_tracking_uri
        or os.getenv("MLFLOW_TRACKING_URI")
        or DEFAULT_MLFLOW_TRACKING_URI
    )
    mlflow.set_experiment(
        experiment_name
        or (
            f"Traffic Forecast - Region {region_id}"
            if region_id is not None
            else "Traffic Forecast"
        )
    )

    raw_df = (
        load_observed_target_data(data_path)
        if data_audit_path is None
        else load_observed_target_data(data_path, audit_path=data_audit_path)
    )
    raw_timestamps = pd.to_datetime(raw_df["date_time"], errors="raise")
    production_after_training = raw_df[raw_timestamps >= pd.Timestamp(train_end_date)]
    df = preprocess(raw_df)
    df = df[
        (df["date_time"] >= train_start_date)
        & (df["date_time"] < train_end_date)
    ].copy()
    _validate_training_data(df)

    data_version = create_data_version(
        df,
        train_start_date,
        train_end_date,
        data_versions_dir=data_versions_dir,
        metadata={
            "region_id": region_id,
            "dataset_id": dataset_id,
            "source_path": data_path,
        },
    )
    train_part, val_part, test_part = split_data(df)
    development_part = pd.concat(
        [train_part, val_part],
        ignore_index=True,
    ).sort_values("date_time")

    print(f"\nTrain size: {len(train_part)}")
    print(f"Validation size: {len(val_part)}")
    print(f"Test size: {len(test_part)}")

    production_start_at = (
        pd.to_datetime(production_after_training["date_time"]).min()
        if not production_after_training.empty
        else None
    )
    production_end_at = (
        pd.to_datetime(production_after_training["date_time"]).max()
        if not production_after_training.empty
        else None
    )

    feature_columns = ["traffic_volume", "date_time"]
    X_development = development_part.drop(feature_columns, axis=1)
    y_development = development_part["traffic_volume"]
    X_test = test_part.drop(feature_columns, axis=1)
    y_test = test_part["traffic_volume"]

    mlflow_dataset = mlflow.data.from_pandas(
        df,
        source=data_path,
        name=(
            f"region_{region_id}_dataset_{dataset_id}"
            if region_id is not None and dataset_id is not None
            else "TrafficVolumeData"
        ),
    )

    trainers = [
        ("RandomForest", train_random_forest),
        ("XGBoost", train_xgboost),
        ("LightGBM", train_lightgbm),
    ]
    results = []
    time_series_split = TimeSeriesSplit(n_splits=cv_splits)

    for model_name, trainer in trainers:
        with mlflow.start_run(run_name=f"{model_role}_{model_name}"):
            mlflow.log_input(mlflow_dataset, context="training")
            fold_metrics = []

            # TimeSeriesSplit always trains on the past and validates on the
            # following period. This is more stable than one validation slice.
            for fold_number, (train_indices, val_indices) in enumerate(
                time_series_split.split(X_development),
                start=1,
            ):
                fold_model = trainer(
                    X_development.iloc[train_indices],
                    y_development.iloc[train_indices],
                    random_state,
                )
                predictions = fold_model.predict(
                    X_development.iloc[val_indices]
                )
                fold_mae, fold_rmse, fold_mape = evaluate(
                    y_development.iloc[val_indices],
                    predictions,
                )
                fold_metrics.append(
                    (fold_mae, fold_rmse, fold_mape)
                )
                mlflow.log_metrics(
                    {
                        f"fold_{fold_number}_MAE": fold_mae,
                        f"fold_{fold_number}_RMSE": fold_rmse,
                        f"fold_{fold_number}_MAPE": fold_mape,
                    }
                )

            mae = float(np.mean([item[0] for item in fold_metrics]))
            rmse = float(np.mean([item[1] for item in fold_metrics]))
            mape = float(np.mean([item[2] for item in fold_metrics]))

            print(
                f"\n{model_name}: "
                f"CV MAE={mae:.2f}, CV RMSE={rmse:.2f}, "
                f"CV MAPE={mape:.2f}%"
            )

            mlflow.set_tags(
                {
                    "model_name": model_name,
                    "model_role": model_role,
                    "data_version": data_version,
                    "region_id": str(region_id) if region_id is not None else "",
                    "dataset_id": str(dataset_id) if dataset_id is not None else "",
                }
            )
            mlflow.log_params(
                {
                    "train_start_date": train_start_date,
                    "train_end_date": train_end_date,
                    "random_state": random_state,
                    "cv_splits": cv_splits,
                    "data_path": data_path,
                }
            )
            mlflow.log_metrics(
                {
                    "cv_MAE": mae,
                    "cv_RMSE": rmse,
                    "cv_MAPE": mape,
                }
            )

            # MAE is also the production drift metric, so selection uses MAE.
            results.append(
                {
                    "name": model_name,
                    "trainer": trainer,
                    "validation_mae": mae,
                    "validation_rmse": rmse,
                    "validation_mape": mape,
                    "validation_mae_std": float(
                        np.std([item[0] for item in fold_metrics])
                    ),
                }
            )

    best = min(results, key=lambda result: result["validation_mae"])
    model_version = create_model_version(
        models_dir=models_dir,
        metadata={
            "region_id": region_id,
            "dataset_id": dataset_id,
            "model_role": model_role,
        },
    )

    # Retrain the selected algorithm on all development data before the final
    # untouched test evaluation.
    best_model = best["trainer"](
        X_development,
        y_development,
        random_state,
    )
    test_predictions = best_model.predict(X_test)
    test_mae, test_rmse, test_mape = evaluate(y_test, test_predictions)

    print(f"\nBest validation-MAE model: {best['name']}")
    print(
        f"Test metrics: "
        f"MAE={test_mae:.2f}, RMSE={test_rmse:.2f}, MAPE={test_mape:.2f}%"
    )

    os.makedirs(models_dir, exist_ok=True)
    versioned_model_path = os.path.join(models_dir, f"{model_version}.pkl")
    joblib.dump(best_model, output_model_path)
    joblib.dump(best_model, versioned_model_path)

    model_info = {
        "best_model_name": best["name"],
        "model_version": model_version,
        "model_role": model_role,
        "region_id": str(region_id) if region_id is not None else None,
        "dataset_id": str(dataset_id) if dataset_id is not None else None,
        "data_path": data_path,
        "data_version": data_version,
        "train_start_date": train_start_date,
        "train_end_date": train_end_date,
        "production_start_at": (
            production_start_at.isoformat()
            if production_start_at is not None
            else None
        ),
        "production_end_at": (
            production_end_at.isoformat()
            if production_end_at is not None
            else None
        ),
        "validation_MAE": round(float(best["validation_mae"]), 4),
        "validation_MAE_std": round(float(best["validation_mae_std"]), 4),
        "validation_RMSE": round(float(best["validation_rmse"]), 4),
        "validation_MAPE": round(float(best["validation_mape"]), 4),
        "cv_splits": int(cv_splits),
        "test_MAE": round(float(test_mae), 4),
        "test_RMSE": round(float(test_rmse), 4),
        "test_MAPE": round(float(test_mape), 4),
        "random_state": int(random_state),
        "saved_at": datetime.now().isoformat(),
        "model_file": output_model_path,
        "versioned_model_file": versioned_model_path,
        "model_comparison": [
            {
                "model_name": result["name"],
                "best_model_name": result["name"],
                "model_family": "legacy_sklearn",
                "validation_MAE": round(float(result["validation_mae"]), 4),
                "validation_MAE_std": round(float(result["validation_mae_std"]), 4),
                "validation_RMSE": round(float(result["validation_rmse"]), 4),
                "validation_MAPE": round(float(result["validation_mape"]), 4),
                "test_MAE": (
                    round(float(test_mae), 4)
                    if result["name"] == best["name"]
                    else None
                ),
                "test_RMSE": (
                    round(float(test_rmse), 4)
                    if result["name"] == best["name"]
                    else None
                ),
                "test_MAPE": (
                    round(float(test_mape), 4)
                    if result["name"] == best["name"]
                    else None
                ),
                "benchmark_only": False,
                "inference_supported": True,
                "selected": result["name"] == best["name"],
            }
            for result in results
        ],
    }
    selected_mlflow_run_id = None
    selected_mlflow_model_uri = None
    with mlflow.start_run(run_name=f"{model_role}_selected_model") as selected_run:
        selected_mlflow_run_id = _get_run_id(selected_run)
        if selected_mlflow_run_id:
            selected_mlflow_model_uri = (
                f"runs:/{selected_mlflow_run_id}/{os.path.basename(output_model_path)}"
            )
        model_info.update(
            {
                "mlflow_run_id": selected_mlflow_run_id,
                "mlflow_model_uri": selected_mlflow_model_uri,
            }
        )
        save_model_info(model_info, output_info_path)
        mlflow.log_input(mlflow_dataset, context="training")
        mlflow.set_tags(
            {
                "model_name": best["name"],
                "model_role": model_role,
                "data_version": data_version,
                "model_version": model_version,
                "region_id": str(region_id) if region_id is not None else "",
                "dataset_id": str(dataset_id) if dataset_id is not None else "",
            }
        )
        mlflow.log_metrics(
            {
                "test_MAE": test_mae,
                "test_RMSE": test_rmse,
                "test_MAPE": test_mape,
            }
        )
        mlflow.log_artifact(output_model_path)
        mlflow.log_artifact(output_info_path)
        try:
            mlflow.sklearn.log_model(
                best_model,
                artifact_path=best["name"],
            )
            if selected_mlflow_run_id:
                selected_mlflow_model_uri = (
                    f"runs:/{selected_mlflow_run_id}/{best['name']}"
                )
                model_info["mlflow_model_uri"] = selected_mlflow_model_uri
                save_model_info(model_info, output_info_path)
        except Exception as exc:
            print(f"MLflow sklearn model logging skipped: {exc}")

    print(f"Model saved: {output_model_path}")
    return model_info

import json
import os
from datetime import datetime

import joblib
import mlflow
import mlflow.data
import mlflow.sklearn
import numpy as np
import pandas as pd
from sklearn.metrics import (
    mean_absolute_error,
    mean_absolute_percentage_error,
    mean_squared_error,
)
from sklearn.model_selection import TimeSeriesSplit

from src.preprocess import load_data, preprocess, split_data
from src.train import (
    train_lightgbm,
    train_random_forest,
    train_xgboost,
)


DATA_PATH = "data/TrafficVolumeData.csv"
DEFAULT_RANDOM_STATE = 42
DEFAULT_CV_SPLITS = 3

os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")


def evaluate(y_true, y_pred):
    """Return the metrics used to compare regression models."""
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    mape = mean_absolute_percentage_error(y_true, y_pred) * 100
    return mae, rmse, mape


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


def create_data_version(df, train_start_date, train_end_date):
    os.makedirs("data_versions", exist_ok=True)
    log_path = "data_versions/version_log.csv"

    if os.path.exists(log_path):
        version_number = len(pd.read_csv(log_path)) + 1
    else:
        version_number = 1

    version_name = f"data_v{version_number}"
    version_file = f"data_versions/{version_name}.csv"
    df.to_csv(version_file, index=False)

    _append_csv_row(
        log_path,
        {
            "version": version_name,
            "train_start": train_start_date,
            "train_end": train_end_date,
            "rows": len(df),
        },
        ["version", "train_start", "train_end", "rows"],
    )

    print(f"\nData version created: {version_name}")
    return version_name


def create_model_version():
    os.makedirs("models", exist_ok=True)
    version_file = "models/model_versions.csv"

    if os.path.exists(version_file):
        version_number = len(pd.read_csv(version_file)) + 1
    else:
        version_number = 1

    model_version = f"model_v{version_number}"
    _append_csv_row(
        version_file,
        {
            "version": model_version,
            "created_at": datetime.now().isoformat(),
        },
        ["version", "created_at"],
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
    output_model_path="models/best_model.pkl",
    output_info_path="models/best_model_info.json",
    model_role="champion",
    random_state=DEFAULT_RANDOM_STATE,
    cv_splits=DEFAULT_CV_SPLITS,
):
    """
    Train all supported models and save the best validation-MAE model.

    Retraining workers should use model_role="candidate" and candidate output
    paths. Only the promotion step is allowed to replace the champion model.
    """
    print(f"\nTraining {model_role} model...")
    print(f"Random state: {random_state}")

    mlflow.set_tracking_uri("file:./mlruns")
    mlflow.set_experiment("Traffic Forecast")

    df = preprocess(load_data(DATA_PATH))
    df = df[
        (df["date_time"] >= train_start_date)
        & (df["date_time"] < train_end_date)
    ].copy()
    _validate_training_data(df)

    data_version = create_data_version(
        df,
        train_start_date,
        train_end_date,
    )
    train_part, val_part, test_part = split_data(df)
    development_part = pd.concat(
        [train_part, val_part],
        ignore_index=True,
    ).sort_values("date_time")

    print(f"\nTrain size: {len(train_part)}")
    print(f"Validation size: {len(val_part)}")
    print(f"Test size: {len(test_part)}")

    feature_columns = ["traffic_volume", "date_time"]
    X_development = development_part.drop(feature_columns, axis=1)
    y_development = development_part["traffic_volume"]
    X_test = test_part.drop(feature_columns, axis=1)
    y_test = test_part["traffic_volume"]

    mlflow_dataset = mlflow.data.from_pandas(
        df,
        source=DATA_PATH,
        name="TrafficVolumeData",
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
                }
            )
            mlflow.log_params(
                {
                    "train_start_date": train_start_date,
                    "train_end_date": train_end_date,
                    "random_state": random_state,
                    "cv_splits": cv_splits,
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
                }
            )

    best = min(results, key=lambda result: result["validation_mae"])
    model_version = create_model_version()

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

    os.makedirs("models", exist_ok=True)
    versioned_model_path = f"models/{model_version}.pkl"
    joblib.dump(best_model, output_model_path)
    joblib.dump(best_model, versioned_model_path)

    model_info = {
        "best_model_name": best["name"],
        "model_version": model_version,
        "model_role": model_role,
        "data_version": data_version,
        "train_start_date": train_start_date,
        "train_end_date": train_end_date,
        "validation_MAE": round(float(best["validation_mae"]), 4),
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
    }
    save_model_info(model_info, output_info_path)

    with mlflow.start_run(run_name=f"{model_role}_selected_model"):
        mlflow.log_input(mlflow_dataset, context="training")
        mlflow.set_tags(
            {
                "model_name": best["name"],
                "model_role": model_role,
                "data_version": data_version,
                "model_version": model_version,
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
        mlflow.sklearn.log_model(best_model, name=best["name"])

    print(f"Model saved: {output_model_path}")
    return model_info

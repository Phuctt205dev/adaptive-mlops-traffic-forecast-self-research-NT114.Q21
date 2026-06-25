import os
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

from src.pipeline import DEFAULT_MLFLOW_TRACKING_URI, evaluate, save_model_info
from src.preprocess import load_observed_target_data, preprocess, split_data

try:
    import mlflow
    import mlflow.keras
except ModuleNotFoundError:
    mlflow = None


DEFAULT_SEQUENCE_LENGTH = 24
DEFAULT_EPOCHS = 8
DEFAULT_BATCH_SIZE = 128


def _region_base_path(artifact_root, region_id):
    if region_id is None:
        return artifact_root
    return os.path.join(artifact_root, "regions", str(region_id))


def _validate_sequence_inputs(df, sequence_length):
    if df.empty:
        raise ValueError("No training data exists in the requested date range.")
    if len(df) <= sequence_length + 7:
        raise ValueError(
            "Training data is too small for recurrent sequence training."
        )


def _make_sequences(features, target, target_indices, sequence_length):
    sequences = []
    labels = []
    for target_index in target_indices:
        start_index = target_index - sequence_length
        if start_index < 0:
            continue
        sequences.append(features[start_index:target_index])
        labels.append(target[target_index])

    if not sequences:
        raise ValueError("No valid recurrent sequences were created.")
    return np.asarray(sequences, dtype=np.float32), np.asarray(labels, dtype=np.float32)


def _metrics_dict(y_true, y_pred):
    mae, rmse, mape = evaluate(y_true, y_pred)
    return {
        "MAE": float(mae),
        "RMSE": float(rmse),
        "MAPE": float(mape),
    }


def _configure_tensorflow_threads(tf):
    try:
        tf.config.threading.set_intra_op_parallelism_threads(
            int(os.getenv("TF_INTRA_OP_THREADS", "1"))
        )
        tf.config.threading.set_inter_op_parallelism_threads(
            int(os.getenv("TF_INTER_OP_THREADS", "1"))
        )
    except RuntimeError:
        pass


def run_recurrent_benchmark(
    model_name,
    train_start_date,
    train_end_date,
    model_role="candidate",
    random_state=42,
    data_path="data/TrafficVolumeData.csv",
    data_audit_path=None,
    region_id=None,
    dataset_id=None,
    artifact_root=".",
    mlflow_tracking_uri=None,
    experiment_name=None,
    sequence_length=DEFAULT_SEQUENCE_LENGTH,
    epochs=DEFAULT_EPOCHS,
    batch_size=DEFAULT_BATCH_SIZE,
    dataset_sha256=None,
    dataset_dvc_rev=None,
    dataset_storage_uri=None,
):
    normalized_model_name = model_name.upper()
    sequence_length = int(sequence_length)
    epochs = int(epochs)
    batch_size = int(batch_size)

    print(f"\nTraining recurrent benchmark: {normalized_model_name}")
    print(f"Sequence length: {sequence_length}")
    print(f"Epochs: {epochs}")
    print(f"Batch size: {batch_size}")

    import tensorflow as tf
    from src.models.recurrent import build_recurrent_model, build_training_callbacks

    _configure_tensorflow_threads(tf)
    tf.keras.utils.set_random_seed(int(random_state))

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
    df = df.sort_values("date_time").reset_index(drop=True)
    _validate_sequence_inputs(df, sequence_length)

    train_part, val_part, test_part = split_data(df)
    train_end_index = len(train_part)
    val_end_index = len(train_part) + len(val_part)

    feature_columns = [
        column for column in df.columns if column not in {"traffic_volume", "date_time"}
    ]
    feature_frame = df[feature_columns].astype(float)
    target_values = df["traffic_volume"].astype(float).to_numpy().reshape(-1, 1)

    feature_scaler = StandardScaler()
    target_scaler = StandardScaler()
    feature_scaler.fit(feature_frame.iloc[:train_end_index])
    target_scaler.fit(target_values[:train_end_index])

    scaled_features = feature_scaler.transform(feature_frame)
    scaled_target = target_scaler.transform(target_values).reshape(-1)

    train_indices = np.arange(sequence_length, train_end_index)
    val_indices = np.arange(max(sequence_length, train_end_index), val_end_index)
    test_indices = np.arange(max(sequence_length, val_end_index), len(df))

    X_train, y_train = _make_sequences(
        scaled_features,
        scaled_target,
        train_indices,
        sequence_length,
    )
    X_val, y_val = _make_sequences(
        scaled_features,
        scaled_target,
        val_indices,
        sequence_length,
    )
    X_test, y_test_scaled = _make_sequences(
        scaled_features,
        scaled_target,
        test_indices,
        sequence_length,
    )

    model = build_recurrent_model(
        normalized_model_name,
        input_shape=(sequence_length, len(feature_columns)),
        recurrent_units=32,
        dense_units=16,
    )
    history = model.fit(
        X_train,
        y_train,
        validation_data=(X_val, y_val),
        epochs=epochs,
        batch_size=batch_size,
        shuffle=False,
        callbacks=build_training_callbacks(),
        verbose=2,
    )

    val_predictions_scaled = model.predict(X_val, verbose=0).reshape(-1, 1)
    test_predictions_scaled = model.predict(X_test, verbose=0).reshape(-1, 1)

    val_predictions = target_scaler.inverse_transform(val_predictions_scaled).reshape(-1)
    test_predictions = target_scaler.inverse_transform(test_predictions_scaled).reshape(-1)
    y_val_true = target_scaler.inverse_transform(y_val.reshape(-1, 1)).reshape(-1)
    y_test_true = target_scaler.inverse_transform(y_test_scaled.reshape(-1, 1)).reshape(-1)

    validation_metrics = _metrics_dict(y_val_true, val_predictions)
    test_metrics = _metrics_dict(y_test_true, test_predictions)

    base_path = _region_base_path(artifact_root, region_id)
    models_dir = os.path.join(base_path, "models", "neural_benchmarks")
    os.makedirs(models_dir, exist_ok=True)

    saved_at = datetime.now().isoformat()
    file_stem = f"{normalized_model_name.lower()}_{model_role}_{saved_at.replace(':', '').replace('-', '')}"
    model_file = os.path.join(models_dir, f"{file_stem}.keras")
    preprocessor_file = os.path.join(models_dir, f"{file_stem}_preprocessors.pkl")
    info_file = os.path.join(models_dir, f"{file_stem}_info.json")

    model.save(model_file)
    joblib.dump(
        {
            "feature_scaler": feature_scaler,
            "target_scaler": target_scaler,
            "feature_columns": feature_columns,
            "sequence_length": sequence_length,
        },
        preprocessor_file,
    )

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

    info = {
        "best_model_name": normalized_model_name,
        "model_version": f"{normalized_model_name.lower()}_benchmark",
        "model_role": model_role,
        "benchmark_only": True,
        "inference_supported": False,
        "region_id": str(region_id) if region_id is not None else None,
        "dataset_id": str(dataset_id) if dataset_id is not None else None,
        "dataset_sha256": dataset_sha256,
        "dataset_dvc_rev": dataset_dvc_rev,
        "dataset_storage_uri": dataset_storage_uri,
        "data_path": data_path,
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
        "sequence_length": sequence_length,
        "epochs": epochs,
        "batch_size": batch_size,
        "recurrent_units": 32,
        "dense_units": 16,
        "validation_MAE": round(validation_metrics["MAE"], 4),
        "validation_RMSE": round(validation_metrics["RMSE"], 4),
        "validation_MAPE": round(validation_metrics["MAPE"], 4),
        "validation_MAE_std": 0.0,
        "test_MAE": round(test_metrics["MAE"], 4),
        "test_RMSE": round(test_metrics["RMSE"], 4),
        "test_MAPE": round(test_metrics["MAPE"], 4),
        "random_state": int(random_state),
        "saved_at": saved_at,
        "model_file": model_file,
        "versioned_model_file": model_file,
        "preprocessor_file": preprocessor_file,
        "history": {
            key: [float(value) for value in values]
            for key, values in history.history.items()
        },
    }

    if mlflow is not None:
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
        with mlflow.start_run(run_name=f"{model_role}_{normalized_model_name}") as run:
            run_id = getattr(getattr(run, "info", None), "run_id", None)
            info["mlflow_run_id"] = run_id
            info["mlflow_model_uri"] = (
                f"runs:/{run_id}/{normalized_model_name}" if run_id else model_file
            )
            mlflow.set_tags(
                {
                    "model_name": normalized_model_name,
                    "model_role": model_role,
                    "benchmark_only": "true",
                    "region_id": str(region_id) if region_id is not None else "",
                    "dataset_id": str(dataset_id) if dataset_id is not None else "",
                    "dataset_dvc_rev": str(dataset_dvc_rev or ""),
                }
            )
            mlflow.log_params(
                {
                    "train_start_date": train_start_date,
                    "train_end_date": train_end_date,
                    "random_state": random_state,
                    "sequence_length": sequence_length,
                    "epochs": epochs,
                    "batch_size": batch_size,
                    "data_path": data_path,
                    "dataset_sha256": str(dataset_sha256 or ""),
                    "dataset_storage_uri": str(dataset_storage_uri or ""),
                    "dataset_dvc_rev": str(dataset_dvc_rev or ""),
                }
            )
            mlflow.log_metrics(
                {
                    "validation_MAE": validation_metrics["MAE"],
                    "validation_RMSE": validation_metrics["RMSE"],
                    "validation_MAPE": validation_metrics["MAPE"],
                    "test_MAE": test_metrics["MAE"],
                    "test_RMSE": test_metrics["RMSE"],
                    "test_MAPE": test_metrics["MAPE"],
                }
            )
            mlflow.log_artifact(preprocessor_file)
            try:
                mlflow.keras.log_model(model, artifact_path=normalized_model_name)
            except Exception as exc:
                print(f"MLflow Keras model logging skipped: {exc}")

    save_model_info(info, info_file)

    print(
        f"\n{normalized_model_name}: "
        f"Validation MAE={validation_metrics['MAE']:.2f}, "
        f"Test MAE={test_metrics['MAE']:.2f}"
    )
    return info

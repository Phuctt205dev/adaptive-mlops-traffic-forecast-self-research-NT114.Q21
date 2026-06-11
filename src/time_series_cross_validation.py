import json
import os
import time
from datetime import datetime

import joblib
import numpy as np
import pandas as pd

from src.lstm_sequences import (
    DEFAULT_SEQUENCE_LENGTH,
    build_sequences,
    fit_sequence_preprocessors,
    prepare_sequence_source,
    transform_sequence_source,
)
from src.models import (
    AUTOREGRESSIVE_MODEL_NAMES,
    build_original_model,
)
from src.time_series_preprocess import TARGET_COLUMN, TIME_COLUMN
from src.time_series_splits import (
    DEFAULT_CV_SPLITS,
    OFFLINE_END,
    PRODUCTION_START,
    create_expanding_window_folds,
    split_development_and_final_test,
    split_offline_and_production,
    summarize_time_range,
    timestamps_to_source_indices,
)
from src.time_series_training import (
    DEFAULT_RANDOM_STATE,
    build_training_pipeline,
    calculate_regression_metrics,
    prepare_feature_data,
    split_features_and_target,
)


RECURRENT_MODEL_NAMES = ("LSTM", "GRU")
SUPPORTED_MODELS = (
    *AUTOREGRESSIVE_MODEL_NAMES,
    *RECURRENT_MODEL_NAMES,
)
TREE_PROFILES = ("autoregressive", "no_lag")


def _save_json(data, path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)
    os.replace(temporary_path, path)


def _save_joblib(model, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary_path = f"{path}.tmp"
    joblib.dump(model, temporary_path)
    os.replace(temporary_path, path)
    return os.path.getsize(path)


def _metric_summary(fold_results):
    """Tính trung bình và độ lệch chuẩn để đo chất lượng lẫn độ ổn định."""
    metric_names = ("MAE", "RMSE", "MAPE", "WAPE", "R2")
    summary = {}
    for metric_name in metric_names:
        values = np.asarray(
            [
                result["validation_metrics"][metric_name]
                for result in fold_results
            ],
            dtype=float,
        )
        summary[metric_name] = {
            "mean": round(float(values.mean()), 4),
            "std": round(float(values.std(ddof=0)), 4),
        }
    return summary


def _fold_metadata(fold):
    return {
        "fold": int(fold["fold"]),
        "train": summarize_time_range(fold["train"]),
        "validation": summarize_time_range(fold["validation"]),
    }


def _save_predictions(timestamps, actual, predictions, path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    frame = pd.DataFrame(
        {
            TIME_COLUMN: pd.to_datetime(timestamps),
            "actual": np.asarray(actual).reshape(-1),
            "prediction": np.asarray(predictions).reshape(-1),
        }
    )
    frame["absolute_error"] = np.abs(
        frame["actual"] - frame["prediction"]
    )
    temporary_path = f"{path}.tmp"
    frame.to_csv(temporary_path, index=False)
    os.replace(temporary_path, path)


def _prepare_offline_features(feature_df):
    prepared = prepare_feature_data(feature_df)
    offline, production = split_offline_and_production(prepared)
    development, final_test = split_development_and_final_test(
        prepared
    )
    return offline, production, development, final_test


def _parse_boolean_series(series, column_name):
    """Đọc cột True/False ổn định dù CSV trả về bool hay chuỗi."""
    if pd.api.types.is_bool_dtype(series):
        return series.astype(bool)

    parsed = (
        series.astype(str)
        .str.strip()
        .str.lower()
        .map({"true": True, "false": False})
    )
    if parsed.isna().any():
        raise ValueError(
            f"Cột {column_name} chứa giá trị không phải True/False."
        )
    return parsed.astype(bool)


def prepare_no_lag_feature_data(
    hourly_df,
    audit_df,
    eligible_timestamps,
):
    """
    Tạo feature thời tiết/lịch, không đưa lag/rolling vào model.

    Timestamp được lấy từ bảng feature autoregressive để cả 8 model cùng làm
    một đề. Chỉ target quan sát thật trong audit được dùng làm nhãn.
    """
    hourly = hourly_df.copy()
    audit = audit_df[[TIME_COLUMN, "target_observed"]].copy()
    hourly[TIME_COLUMN] = pd.to_datetime(
        hourly[TIME_COLUMN],
        errors="raise",
    )
    audit[TIME_COLUMN] = pd.to_datetime(
        audit[TIME_COLUMN],
        errors="raise",
    )
    audit["target_observed"] = _parse_boolean_series(
        audit["target_observed"],
        "target_observed",
    )
    if hourly[TIME_COLUMN].duplicated().any():
        raise ValueError("Hourly data có date_time bị trùng.")
    if audit[TIME_COLUMN].duplicated().any():
        raise ValueError("Audit data có date_time bị trùng.")

    merged = hourly.merge(
        audit,
        on=TIME_COLUMN,
        how="left",
        validate="one_to_one",
    )
    merged["target_observed"] = (
        merged["target_observed"].fillna(False).astype(bool)
    )
    eligible = pd.DatetimeIndex(
        pd.to_datetime(eligible_timestamps)
    )
    result = merged.loc[
        merged[TIME_COLUMN].isin(eligible)
        & merged["target_observed"]
    ].copy()

    result["is_holiday"] = (
        result["is_holiday"].notna().astype(int)
    )
    result["hour"] = result[TIME_COLUMN].dt.hour
    result["day"] = result[TIME_COLUMN].dt.dayofweek
    result["month"] = result[TIME_COLUMN].dt.month
    result["hour_sin"] = np.sin(
        2 * np.pi * result["hour"] / 24
    )
    result["hour_cos"] = np.cos(
        2 * np.pi * result["hour"] / 24
    )
    result["is_weekend"] = result["day"].isin([5, 6]).astype(int)
    result = result.drop(
        columns=[
            "weather_description",
            "target_observed",
        ],
        errors="ignore",
    )
    result = result.sort_values(TIME_COLUMN).reset_index(drop=True)

    missing = eligible.difference(
        pd.DatetimeIndex(result[TIME_COLUMN])
    )
    if not missing.empty:
        raise ValueError(
            "No-lag data không khớp timestamp chung đầu tiên: "
            f"{missing[0]}"
        )
    return result


def build_no_lag_training_pipeline(
    model_name,
    feature_columns,
    random_state,
):
    """Đóng gói preprocessing và model cây gốc không có lag/rolling."""
    from sklearn.pipeline import Pipeline

    from src.time_series_training import build_preprocessor

    return Pipeline(
        steps=[
            ("preprocessor", build_preprocessor(feature_columns)),
            (
                "model",
                build_original_model(model_name, random_state),
            ),
        ]
    )


def _run_tree_cross_validation(
    model_name,
    feature_df,
    n_splits,
    random_state,
    model_path,
    predictions_path,
    profile="autoregressive",
    hourly_df=None,
    audit_df=None,
):
    if profile not in TREE_PROFILES:
        raise ValueError(
            f"Tree profile không hỗ trợ: {profile}."
        )

    autoregressive_data = prepare_feature_data(feature_df)
    if profile == "no_lag":
        if hourly_df is None or audit_df is None:
            raise ValueError(
                "Model no-lag cần hourly_df và audit_df."
            )
        training_data = prepare_no_lag_feature_data(
            hourly_df,
            audit_df,
            autoregressive_data[TIME_COLUMN],
        )
        offline, production = split_offline_and_production(
            training_data
        )
        development, final_test = (
            split_development_and_final_test(training_data)
        )
        pipeline_builder = build_no_lag_training_pipeline
    else:
        offline, production, development, final_test = (
            _prepare_offline_features(autoregressive_data)
        )
        pipeline_builder = build_training_pipeline

    folds = create_expanding_window_folds(
        development,
        n_splits=n_splits,
    )

    fold_results = []
    for fold in folds:
        X_train, y_train, _ = split_features_and_target(
            fold["train"]
        )
        X_validation, y_validation, _ = (
            split_features_and_target(fold["validation"])
        )
        pipeline = pipeline_builder(
            model_name,
            feature_columns=list(X_train.columns),
            random_state=random_state,
        )

        started_at = time.perf_counter()
        pipeline.fit(X_train, y_train)
        training_seconds = time.perf_counter() - started_at
        predictions = pipeline.predict(X_validation)
        fold_results.append(
            {
                **_fold_metadata(fold),
                "validation_metrics": (
                    calculate_regression_metrics(
                        y_validation,
                        predictions,
                    )
                ),
                "training_seconds": round(
                    training_seconds,
                    4,
                ),
            }
        )

    # Sau CV, model cuối học toàn bộ Development. Final Test chưa từng tham
    # gia fit, chọn model hoặc chuẩn hóa dữ liệu.
    X_development, y_development, _ = split_features_and_target(
        development
    )
    X_test, y_test, test_timestamps = split_features_and_target(
        final_test
    )
    final_model = pipeline_builder(
        model_name,
        feature_columns=list(X_development.columns),
        random_state=random_state,
    )
    started_at = time.perf_counter()
    final_model.fit(X_development, y_development)
    final_training_seconds = time.perf_counter() - started_at
    test_predictions = final_model.predict(X_test)
    model_size_bytes = _save_joblib(final_model, model_path)
    _save_predictions(
        test_timestamps,
        y_test,
        test_predictions,
        predictions_path,
    )
    return {
        "family": (
            "tree_autoregressive"
            if profile == "autoregressive"
            else "tree_no_lag"
        ),
        "tree_profile": profile,
        "offline": summarize_time_range(offline),
        "development": summarize_time_range(development),
        "final_test": summarize_time_range(final_test),
        "production_reserved": summarize_time_range(production),
        "folds": fold_results,
        "cv_metrics": _metric_summary(fold_results),
        "final_test_metrics": calculate_regression_metrics(
            y_test,
            test_predictions,
        ),
        "final_training_seconds": round(
            final_training_seconds,
            4,
        ),
        "model_path": model_path,
        "model_size_bytes": int(model_size_bytes),
        "predictions_path": predictions_path,
    }


def _build_neural_fold_datasets(
    source_df,
    train_timestamps,
    validation_timestamps,
    sequence_length,
):
    train_indices = timestamps_to_source_indices(
        source_df,
        train_timestamps,
    )
    validation_indices = timestamps_to_source_indices(
        source_df,
        validation_timestamps,
    )
    if train_indices.min() < sequence_length:
        raise ValueError(
            "Target train đầu tiên chưa có đủ lịch sử sequence."
        )

    preprocessors = fit_sequence_preprocessors(
        source_df,
        train_indices,
    )
    transformed = transform_sequence_source(
        source_df,
        preprocessors,
    )
    datasets = {}
    for name, indices in (
        ("train", train_indices),
        ("validation", validation_indices),
    ):
        X, y, raw_y, timestamps = build_sequences(
            transformed,
            source_df,
            indices,
            preprocessors["target_scaler"],
            sequence_length=sequence_length,
        )
        datasets[name] = {
            "X": X,
            "y": y,
            "raw_y": raw_y,
            "timestamps": timestamps,
        }
    return datasets, preprocessors


def _train_neural_fold(
    model_name,
    datasets,
    target_scaler,
    max_epochs,
    batch_size,
    random_state,
    verbose,
):
    # TensorFlow chỉ được import khi người dùng thực sự chạy LSTM/GRU.
    from tensorflow import keras

    from src.models.recurrent import (
        build_recurrent_model,
        build_training_callbacks,
    )
    from src.neural_utils import (
        configure_reproducibility,
        inverse_scale_predictions,
    )

    keras.backend.clear_session()
    configure_reproducibility(random_state)
    model = build_recurrent_model(
        model_name,
        input_shape=datasets["train"]["X"].shape[1:],
    )
    started_at = time.perf_counter()
    history = model.fit(
        datasets["train"]["X"],
        datasets["train"]["y"],
        validation_data=(
            datasets["validation"]["X"],
            datasets["validation"]["y"],
        ),
        epochs=max_epochs,
        batch_size=batch_size,
        callbacks=build_training_callbacks(),
        shuffle=False,
        verbose=verbose,
    )
    training_seconds = time.perf_counter() - started_at
    scaled_predictions = model.predict(
        datasets["validation"]["X"],
        batch_size=256,
        verbose=0,
    )
    predictions = inverse_scale_predictions(
        scaled_predictions,
        target_scaler,
    )
    metrics = calculate_regression_metrics(
        datasets["validation"]["raw_y"].reshape(-1),
        predictions,
    )
    best_epoch = int(np.argmin(history.history["val_loss"]) + 1)
    trained_epochs = int(len(history.epoch))
    keras.backend.clear_session()
    return metrics, best_epoch, trained_epochs, training_seconds


def _train_final_neural_model(
    model_name,
    source_df,
    development,
    final_test,
    sequence_length,
    epochs,
    batch_size,
    random_state,
    model_path,
    preprocessor_path,
    predictions_path,
    verbose,
):
    from tensorflow import keras

    from src.models.recurrent import build_recurrent_model
    from src.neural_utils import (
        configure_reproducibility,
        inverse_scale_predictions,
    )

    datasets, preprocessors = _build_neural_fold_datasets(
        source_df,
        development[TIME_COLUMN],
        final_test[TIME_COLUMN],
        sequence_length,
    )
    keras.backend.clear_session()
    configure_reproducibility(random_state)
    model = build_recurrent_model(
        model_name,
        input_shape=datasets["train"]["X"].shape[1:],
    )
    started_at = time.perf_counter()
    model.fit(
        datasets["train"]["X"],
        datasets["train"]["y"],
        epochs=epochs,
        batch_size=batch_size,
        shuffle=False,
        verbose=verbose,
    )
    training_seconds = time.perf_counter() - started_at
    scaled_predictions = model.predict(
        datasets["validation"]["X"],
        batch_size=256,
        verbose=0,
    )
    predictions = inverse_scale_predictions(
        scaled_predictions,
        preprocessors["target_scaler"],
    )

    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    temporary_path = model_path.replace(".keras", ".tmp.keras")
    model.save(temporary_path)
    os.replace(temporary_path, model_path)
    model_size_bytes = os.path.getsize(model_path)
    preprocessor_size_bytes = _save_joblib(
        preprocessors,
        preprocessor_path,
    )
    actual = datasets["validation"]["raw_y"].reshape(-1)
    _save_predictions(
        datasets["validation"]["timestamps"],
        actual,
        predictions,
        predictions_path,
    )
    metrics = calculate_regression_metrics(actual, predictions)
    keras.backend.clear_session()
    return {
        "metrics": metrics,
        "training_seconds": round(training_seconds, 4),
        "model_size_bytes": int(model_size_bytes),
        "preprocessor_path": preprocessor_path,
        "preprocessor_size_bytes": int(
            preprocessor_size_bytes
        ),
    }


def _run_neural_cross_validation(
    model_name,
    feature_df,
    hourly_df,
    audit_df,
    n_splits,
    sequence_length,
    max_epochs,
    batch_size,
    random_state,
    model_path,
    preprocessor_path,
    predictions_path,
    verbose,
):
    offline, production, development, final_test = (
        _prepare_offline_features(feature_df)
    )
    folds = create_expanding_window_folds(
        development,
        n_splits=n_splits,
    )

    # Cắt production trước khi tạo feature causal để luồng neural hoàn toàn
    # không xử lý các giá trị từ năm 2016 trong giai đoạn nghiên cứu offline.
    hourly_times = pd.to_datetime(
        hourly_df[TIME_COLUMN],
        errors="raise",
    )
    audit_times = pd.to_datetime(
        audit_df[TIME_COLUMN],
        errors="raise",
    )
    offline_hourly = hourly_df.loc[
        hourly_times <= OFFLINE_END
    ].copy()
    offline_audit = audit_df.loc[
        audit_times <= OFFLINE_END
    ].copy()
    source = prepare_sequence_source(
        offline_hourly,
        offline_audit,
    ).reset_index(drop=True)

    fold_results = []
    best_epochs = []
    for fold in folds:
        datasets, preprocessors = _build_neural_fold_datasets(
            source,
            fold["train"][TIME_COLUMN],
            fold["validation"][TIME_COLUMN],
            sequence_length,
        )
        metrics, best_epoch, trained_epochs, training_seconds = (
            _train_neural_fold(
                model_name,
                datasets,
                preprocessors["target_scaler"],
                max_epochs,
                batch_size,
                random_state,
                verbose,
            )
        )
        best_epochs.append(best_epoch)
        fold_results.append(
            {
                **_fold_metadata(fold),
                "validation_metrics": metrics,
                "best_epoch": int(best_epoch),
                "epochs_trained": int(trained_epochs),
                "training_seconds": round(
                    training_seconds,
                    4,
                ),
            }
        )

    # Median làm giảm ảnh hưởng của một fold quá dễ hoặc quá khó.
    final_epochs = max(1, int(round(float(np.median(best_epochs)))))
    final_result = _train_final_neural_model(
        model_name,
        source,
        development,
        final_test,
        sequence_length,
        final_epochs,
        batch_size,
        random_state,
        model_path,
        preprocessor_path,
        predictions_path,
        verbose,
    )
    return {
        "family": "neural_sequence",
        "offline": summarize_time_range(offline),
        "development": summarize_time_range(development),
        "final_test": summarize_time_range(final_test),
        "production_reserved": summarize_time_range(production),
        "sequence_length_hours": int(sequence_length),
        "max_epochs_per_fold": int(max_epochs),
        "batch_size": int(batch_size),
        "folds": fold_results,
        "cv_metrics": _metric_summary(fold_results),
        "final_epoch_policy": "median_best_epoch_from_cv_folds",
        "final_epochs": int(final_epochs),
        "final_test_metrics": final_result["metrics"],
        "final_training_seconds": final_result[
            "training_seconds"
        ],
        "model_path": model_path,
        "model_size_bytes": final_result["model_size_bytes"],
        "preprocessor_path": final_result[
            "preprocessor_path"
        ],
        "preprocessor_size_bytes": final_result[
            "preprocessor_size_bytes"
        ],
        "predictions_path": predictions_path,
    }


def run_model_time_series_cross_validation(
    model_name,
    feature_df,
    hourly_df=None,
    audit_df=None,
    n_splits=DEFAULT_CV_SPLITS,
    sequence_length=DEFAULT_SEQUENCE_LENGTH,
    max_epochs=20,
    batch_size=128,
    random_state=DEFAULT_RANDOM_STATE,
    output_directory="models/time_series/cross_validation",
    result_directory="results/time_series_cross_validation",
    verbose=2,
    tree_profile="autoregressive",
    artifact_name=None,
):
    """Chạy CV và Final Test cho đúng một model."""
    if model_name not in SUPPORTED_MODELS:
        raise ValueError(
            f"Model không hỗ trợ: {model_name}. "
            f"Chỉ dùng: {', '.join(SUPPORTED_MODELS)}."
        )

    normalized_name = artifact_name or model_name.lower()
    extension = (
        ".keras"
        if model_name in RECURRENT_MODEL_NAMES
        else ".pkl"
    )
    model_path = os.path.join(
        output_directory,
        f"{normalized_name}_final{extension}",
    )
    preprocessor_path = os.path.join(
        output_directory,
        f"{normalized_name}_preprocessors.pkl",
    )
    predictions_path = os.path.join(
        result_directory,
        f"{normalized_name}_final_test_predictions.csv",
    )
    report_path = os.path.join(
        result_directory,
        f"{normalized_name}_report.json",
    )
    folds_path = os.path.join(
        result_directory,
        f"{normalized_name}_folds.csv",
    )

    if model_name in RECURRENT_MODEL_NAMES:
        if hourly_df is None or audit_df is None:
            raise ValueError(
                "LSTM/GRU cần hourly_df và audit_df."
            )
        result = _run_neural_cross_validation(
            model_name,
            feature_df,
            hourly_df,
            audit_df,
            n_splits,
            sequence_length,
            max_epochs,
            batch_size,
            random_state,
            model_path,
            preprocessor_path,
            predictions_path,
            verbose,
        )
    else:
        result = _run_tree_cross_validation(
            model_name,
            feature_df,
            n_splits,
            random_state,
            model_path,
            predictions_path,
            profile=tree_profile,
            hourly_df=hourly_df,
            audit_df=audit_df,
        )

    report = {
        "created_at": datetime.now().isoformat(),
        "model": model_name,
        "variant": normalized_name,
        "selection_metric": "cross_validation_mean_MAE",
        "split_policy": {
            "development_end": "2015-09-30T23:00:00",
            "final_test_start": "2015-10-01T00:00:00",
            "offline_end": OFFLINE_END.isoformat(),
            "production_start": PRODUCTION_START.isoformat(),
            "cv_method": "expanding_window",
            "cv_splits": int(n_splits),
            "shuffle": False,
        },
        "production_used_for_training": False,
        "random_state": int(random_state),
        "training_configuration": {
            "cv_splits": int(n_splits),
            "random_state": int(random_state),
            "sequence_length_hours": int(sequence_length),
            "max_epochs": (
                int(max_epochs)
                if model_name in RECURRENT_MODEL_NAMES
                else None
            ),
            "batch_size": (
                int(batch_size)
                if model_name in RECURRENT_MODEL_NAMES
                else None
            ),
        },
        **result,
        "report_path": report_path,
        "folds_path": folds_path,
        "artifact_paths": (
            [
                result["model_path"],
                result["preprocessor_path"],
            ]
            if model_name in RECURRENT_MODEL_NAMES
            else [result["model_path"]]
        ),
    }

    fold_rows = []
    for fold in report["folds"]:
        fold_rows.append(
            {
                "fold": fold["fold"],
                "train_start": fold["train"]["start"],
                "train_end": fold["train"]["end"],
                "train_rows": fold["train"]["rows"],
                "validation_start": fold["validation"]["start"],
                "validation_end": fold["validation"]["end"],
                "validation_rows": fold["validation"]["rows"],
                **fold["validation_metrics"],
                "training_seconds": fold["training_seconds"],
            }
        )
    os.makedirs(result_directory, exist_ok=True)
    pd.DataFrame(fold_rows).to_csv(folds_path, index=False)
    _save_json(report, report_path)
    return report

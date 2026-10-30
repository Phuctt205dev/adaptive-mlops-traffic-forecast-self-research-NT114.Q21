import json
import os
import time
from datetime import datetime

import joblib
import numpy as np
import pandas as pd

from src.mlflow_regions import set_mlflow_experiment
from src.shap_reporting import explain_saved_model, file_sha256, resolve_shap_config, neural_shap_config
from src.feature_selection import (validate_selection, reusable_groups, rank_training_features,
                                  columns_for_groups, selection_summary, MODEL_KEYS, guard_quality_holdout)
from src.training_progress import epoch_callbacks, report as report_progress, set_fold

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
from src.time_series_features import DEFAULT_LAG_HOURS
from src.time_series_splits import (
    DEFAULT_CV_SPLITS,
    DEVELOPMENT_END,
    FINAL_TEST_START,
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


def _get_run_id(run):
    return getattr(getattr(run, "info", None), "run_id", None)


def _load_mlflow():
    try:
        import mlflow
    except ModuleNotFoundError:
        return None
    return mlflow


def _build_mlflow_dataset(mlflow, dataframe, dataset_name=None, dataset_source=None):
    try:
        import mlflow.data

        return mlflow.data.from_pandas(
            dataframe,
            source=dataset_source or "traffic_training_dataframe",
            name=dataset_name or "TrafficVolumeData",
        )
    except Exception as exc:
        print(f"MLflow dataset logging skipped: {exc}")
        return None


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


def _split_by_dynamic_window(
    dataframe,
    train_start_date,
    train_end_date,
    final_test_ratio=0.15,
):
    ordered = prepare_feature_data(dataframe)
    train_start = pd.Timestamp(train_start_date)
    train_end = pd.Timestamp(train_end_date)
    if train_start >= train_end:
        raise ValueError("train_start_date phai nho hon train_end_date.")

    offline = ordered.loc[
        (ordered[TIME_COLUMN] >= train_start)
        & (ordered[TIME_COLUMN] < train_end)
    ].copy()
    production = ordered.loc[ordered[TIME_COLUMN] >= train_end].copy()
    if offline.empty:
        raise ValueError("Training window khong co du lieu offline.")
    if not 0 < final_test_ratio < 0.5:
        raise ValueError("final_test_ratio phai nam trong khoang (0, 0.5).")

    final_test_rows = max(1, int(round(len(offline) * final_test_ratio)))
    development_rows = len(offline) - final_test_rows
    if development_rows <= 1:
        raise ValueError("Training window qua ngan de tao Development va Final Test.")

    development = offline.iloc[:development_rows].copy()
    final_test = offline.iloc[development_rows:].copy()
    return offline, production, development, final_test


def _prepare_offline_features(
    feature_df,
    train_start_date=None,
    train_end_date=None,
    final_test_ratio=0.15,
):
    prepared = prepare_feature_data(feature_df)
    if train_start_date is not None and train_end_date is not None:
        return _split_by_dynamic_window(
            prepared,
            train_start_date,
            train_end_date,
            final_test_ratio=final_test_ratio,
        )
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
    train_start_date=None,
    train_end_date=None,
    final_test_ratio=0.15,
    shap_config=None,
    shap_context=None,
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
        offline, production, development, final_test = (
            _prepare_offline_features(
                training_data,
                train_start_date=train_start_date,
                train_end_date=train_end_date,
                final_test_ratio=final_test_ratio,
            )
        )
        pipeline_builder = build_no_lag_training_pipeline
    else:
        offline, production, development, final_test = (
            _prepare_offline_features(
                autoregressive_data,
                train_start_date=train_start_date,
                train_end_date=train_end_date,
                final_test_ratio=final_test_ratio,
            )
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

        del pipeline

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
    shap_report = explain_saved_model(
        final_model, X_development, model_name=model_name, model_path=model_path,
        output_path=f"{model_path}.shap.json", config=shap_config,
        context=shap_context, timestamps=development[TIME_COLUMN],
    )
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
        "shap_report": shap_report,
        "predictions_path": predictions_path,
    }


def _build_neural_fold_datasets(
    source_df,
    train_timestamps,
    validation_timestamps,
    sequence_length,
    selected_groups=None,
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
        source_df.iloc[_sequence_preprocessor_start(source_df):],
        train_indices - _sequence_preprocessor_start(source_df),
    )
    if selected_groups:
        names = preprocessors["sequence_feature_names"]
        selected = columns_for_groups(names, selected_groups)
        if not selected:
            raise ValueError("No sequence features match the selected groups.")
        preprocessors["selected_feature_indices"] = [names.index(name) for name in selected]
        preprocessors["all_sequence_feature_names"] = names
        preprocessors["sequence_feature_names"] = selected
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
    selection_config=None,
    selection_row=None,
    feature_names=None,
    shap_config=None,
    inner_validation=False,
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
    fit_X, fit_y = datasets["train"]["X"], datasets["train"]["y"]
    validation = (datasets["validation"]["X"], datasets["validation"]["y"])
    if selection_row is not None or inner_validation:
        # A selector must not inherit weights/epochs chosen on the outer
        # validation labels. Both alternatives stop on the same inner tail.
        if len(fit_X) < 2:
            raise ValueError("Feature selection needs at least two training sequences.")
        split = max(1, min(len(fit_X) - 1, int(len(fit_X) * .9)))
        validation = (fit_X[split:], fit_y[split:])
        fit_X, fit_y = fit_X[:split], fit_y[:split]
    history = model.fit(
        fit_X,
        fit_y,
        validation_data=validation,
        epochs=max_epochs,
        batch_size=batch_size,
        callbacks=build_training_callbacks() + epoch_callbacks(model_name, max_epochs),
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
    if selection_row is not None:
        # Explicit ordering matches calculate_regression_metrics, independent of dict order.
        selection_row["full_metrics"] = [metrics[key] for key in ("MAE", "RMSE", "MAPE")]
        selection_row["reduced_metrics"] = None
        selection_row["status"] = "full"
        selection_row["early_stopping_source"] = "inner_training_tail"
        try:
            report_progress(model_name, "explaining")
            groups = rank_training_features(model, datasets["train"]["X"], feature_names, sequence=True,
                        shap_config=shap_config, retain_share=selection_config.get("retain_share", .95))
            selected = columns_for_groups(feature_names, groups)
            selection_row["selected_groups"] = groups
        except (ValueError, RuntimeError, MemoryError, TimeoutError, ImportError) as error:
            selection_row["reason"] = type(error).__name__
            selected = list(feature_names)
        if len(selected) < len(feature_names):
            indices = [feature_names.index(name) for name in selected]
            del model
            del history
            keras.backend.clear_session()
            import gc
            gc.collect()
            # Replace the caller's arrays after the full model is released,
            # instead of retaining full and reduced sequence tensors together.
            del fit_X, fit_y, validation
            for value in datasets.values():
                value["X"] = value["X"][:, :, indices]
            reduced_metrics, reduced_epoch, reduced_epochs, reduced_seconds = _train_neural_fold(
                model_name, datasets, target_scaler, max_epochs, batch_size, random_state, verbose, inner_validation=True)
            selection_row.update(reduced_metrics=[reduced_metrics[key] for key in ("MAE", "RMSE", "MAPE")],
                                 reduced_validation_metrics=dict(reduced_metrics),
                                 reduced_best_epoch=reduced_epoch, reduced_epochs=reduced_epochs,
                                 reduced_training_seconds=reduced_seconds, status="compared")
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
    shap_config=None,
    shap_context=None,
    selected_groups=None,
):
    from tensorflow import keras

    set_fold()
    report_progress(model_name, "final_fit_started")

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
        selected_groups=selected_groups,
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
        callbacks=epoch_callbacks(model_name, epochs),
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

    from src.training_progress import enabled as progress_enabled
    compatibility_file = None
    if progress_enabled():
        os.makedirs(os.path.dirname(model_path), exist_ok=True)
        compatibility_file = model_path + ".compatibility.npz"
        np.savez_compressed(compatibility_file, X=datasets["validation"]["X"][:4],
                            expected=scaled_predictions[:4], raw_expected=predictions[:4])

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
    report_progress(model_name, "explaining")
    shap_report = explain_saved_model(
        model, datasets["train"]["X"], model_name=model_name, model_path=model_path,
        output_path=f"{model_path}.shap.json", sequence=True, config=shap_config,
        context={**(shap_context or {}), "preprocessor_sha256": file_sha256(preprocessor_path)},
        feature_names=preprocessors["sequence_feature_names"],
        target_scale=float(preprocessors["target_scaler"].scale_[0]),
        timestamps=datasets["train"]["timestamps"],
    )
    keras.backend.clear_session()
    return {
        "metrics": metrics,
        "training_seconds": round(training_seconds, 4),
        "model_size_bytes": int(model_size_bytes),
        "preprocessor_path": preprocessor_path,
        "preprocessor_size_bytes": int(
            preprocessor_size_bytes
        ),
        "shap_report": shap_report,
        "selected_feature_names": preprocessors["sequence_feature_names"],
        **({"compatibility_file": compatibility_file} if compatibility_file else {}),
    }


def _sequence_preprocessor_start(source_df):
    start = source_df.attrs.get("preprocessor_start_at")
    if start is None:
        return 0
    times = pd.to_datetime(source_df[TIME_COLUMN]).to_numpy(dtype="datetime64[ns]")
    return int(np.searchsorted(times, pd.Timestamp(start).to_datetime64()))


def _prepare_neural_sequence_source(hourly_df, audit_df, offline_start, offline_end, sequence_length):
    """Keep pre-window history as context, not as extra training targets."""
    offline_start, offline_end = pd.Timestamp(offline_start), pd.Timestamp(offline_end)
    # Causal traffic may need a past-week fallback before the first input
    # sequence. Keep that warmup too, but do not use it to fit the scalers.
    context_start = offline_start - pd.Timedelta(hours=sequence_length + max(DEFAULT_LAG_HOURS))
    hourly_times = pd.to_datetime(hourly_df[TIME_COLUMN], errors="raise")
    audit_times = pd.to_datetime(audit_df[TIME_COLUMN], errors="raise")
    hourly = hourly_df.loc[(hourly_times >= context_start) & (hourly_times <= offline_end)].copy()
    audit = audit_df.loc[(audit_times >= context_start) & (audit_times <= offline_end)].copy()
    source = prepare_sequence_source(hourly, audit).reset_index(drop=True)
    source.attrs["preprocessor_start_at"] = (offline_start - pd.Timedelta(hours=sequence_length)).isoformat()
    return source


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
    train_start_date=None,
    train_end_date=None,
    final_test_ratio=0.15,
    shap_config=None,
    shap_context=None,
    feature_selection=None,
):
    offline, production, development, final_test = (
        _prepare_offline_features(
            feature_df,
            train_start_date=train_start_date,
            train_end_date=train_end_date,
            final_test_ratio=final_test_ratio,
        )
    )
    folds = create_expanding_window_folds(
        development,
        n_splits=n_splits,
    )

    # Cắt production trước khi tạo feature causal để luồng neural hoàn toàn
    # không xử lý các giá trị từ năm 2016 trong giai đoạn nghiên cứu offline.
    offline_end = pd.Timestamp(offline[TIME_COLUMN].max())
    offline_start = (
        pd.Timestamp(train_start_date)
        if train_start_date is not None
        else pd.Timestamp(offline[TIME_COLUMN].min())
    )
    source = _prepare_neural_sequence_source(hourly_df, audit_df, offline_start, offline_end, sequence_length)

    selection_config = guard_quality_holdout(feature_selection, final_test[TIME_COLUMN].min())
    selecting = selection_config.get("enabled", False)
    selection_folds = []
    cached_groups = None
    fold_results = []
    best_epochs = []
    for fold in folds:
        set_fold(len(fold_results) + 1, len(folds))
        report_progress(model_name, "fold_started")
        datasets, preprocessors = _build_neural_fold_datasets(
            source,
            fold["train"][TIME_COLUMN],
            fold["validation"][TIME_COLUMN],
            sequence_length,
        )
        names = preprocessors["sequence_feature_names"]
        if selecting and not selection_folds:
            cached_groups = reusable_groups(selection_config, model_name, names, fold["train"][TIME_COLUMN].max())
            if cached_groups and selection_config["profiles"][MODEL_KEYS[model_name]].get("sequence_length") != sequence_length:
                cached_groups = None
        if cached_groups:
            indices = [names.index(name) for name in columns_for_groups(names, cached_groups)]
            datasets = {key: {**value, "X": value["X"][:, :, indices]} for key, value in datasets.items()}
        row = {"train_end": str(fold["train"][TIME_COLUMN].max()),
               "validation_start": str(fold["validation"][TIME_COLUMN].min()),
               "selected_groups": cached_groups}
        selection_kwargs = ({"selection_config": selection_config, "selection_row": row,
                             "feature_names": names, "shap_config": shap_config}
                            if selecting and not cached_groups else {})
        started = time.perf_counter()
        metrics, best_epoch, trained_epochs, training_seconds = (
            _train_neural_fold(
                model_name,
                datasets,
                preprocessors["target_scaler"],
                max_epochs,
                batch_size,
                random_state,
                verbose,
                **selection_kwargs,
            )
        )
        if selecting:
            if cached_groups:
                row.update(full_metrics=None, reduced_metrics=[metrics[key] for key in ("MAE", "RMSE", "MAPE")],
                           reduced_validation_metrics=dict(metrics),
                           status="reused", reduced_best_epoch=best_epoch)
            row["total_seconds"] = round(time.perf_counter() - started, 4)
            selection_folds.append(row)
        best_epochs.append(best_epoch)
        report_progress(model_name, "fold_completed")
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

    model_selection = None
    if selecting:
        model_selection = selection_summary(model_name, selection_config, selection_folds, names,
            selection_train_end=selection_folds[-1]["train_end"], reused=bool(cached_groups))
        if cached_groups:
            model_selection["selection_train_end"] = selection_config["profiles"][MODEL_KEYS[model_name]]["selection_train_end"]
        model_selection["sequence_length"] = sequence_length
        if model_selection["mode"] == "reduced":
            best_epochs = [row["reduced_best_epoch"] for row in selection_folds]
            for fold_result, selection_row in zip(fold_results, selection_folds):
                fold_result["validation_metrics"] = dict(selection_row["reduced_validation_metrics"])
                fold_result["best_epoch"] = selection_row["reduced_best_epoch"]
                fold_result["epochs_trained"] = selection_row.get("reduced_epochs", fold_result["epochs_trained"])
                fold_result["training_seconds"] += selection_row.get("reduced_training_seconds", 0.)
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
        shap_config=shap_config,
        shap_context=shap_context,
        **({"selected_groups": model_selection["selected_groups"]}
           if model_selection and model_selection["mode"] == "reduced" else {}),
    )
    return {
        "family": "neural_sequence",
        "offline": summarize_time_range(offline),
        "development": summarize_time_range(development),
        "final_test": summarize_time_range(final_test),
        "production_reserved": summarize_time_range(production),
        "sequence_length_hours": int(sequence_length),
        "history_context": {"start": str(source[TIME_COLUMN].min()), "targets_start": str(offline_start),
                            "context_is_not_training_targets": True},
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
        **({"compatibility_file": final_result["compatibility_file"]} if final_result.get("compatibility_file") else {}),
        "model_path": model_path,
        "model_size_bytes": final_result["model_size_bytes"],
        "preprocessor_path": final_result[
            "preprocessor_path"
        ],
        "preprocessor_size_bytes": final_result[
            "preprocessor_size_bytes"
        ],
        "shap_report": final_result["shap_report"],
        **({"feature_selection_result": {**model_selection, "selected_features": final_result["selected_feature_names"]}}
           if model_selection else {}),
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
    train_start_date=None,
    train_end_date=None,
    final_test_ratio=0.15,
    mlflow_tracking_uri=None,
    experiment_name=None,
    mlflow_dataset_name=None,
    mlflow_dataset_source=None,
    shap_config=None,
    shap_context=None,
    feature_selection=None,
):
    """Chạy CV và Final Test cho đúng một model."""
    if model_name not in SUPPORTED_MODELS:
        raise ValueError(
            f"Model không hỗ trợ: {model_name}. "
            f"Chỉ dùng: {', '.join(SUPPORTED_MODELS)}."
        )

    shap_config = (neural_shap_config(shap_config) if model_name in {"LSTM", "GRU"}
                   else resolve_shap_config(shap_config))
    shap_context = {**(shap_context or {}), "train_start_date": str(train_start_date),
                    "train_end_date": str(train_end_date)}
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
            train_start_date=train_start_date,
            train_end_date=train_end_date,
            final_test_ratio=final_test_ratio,
            shap_config=shap_config,
            shap_context=shap_context,
            feature_selection=feature_selection,
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
            train_start_date=train_start_date,
            train_end_date=train_end_date,
            final_test_ratio=final_test_ratio,
            shap_config=shap_config,
            shap_context=shap_context,
        )

    split_policy = {
        "cv_method": "expanding_window",
        "cv_splits": int(n_splits),
        "shuffle": False,
    }
    if train_start_date is not None and train_end_date is not None:
        split_policy.update(
            {
                "mode": "dynamic_training_window",
                "train_start_date": pd.Timestamp(train_start_date).isoformat(),
                "train_end_date": pd.Timestamp(train_end_date).isoformat(),
                "development_start": result["development"]["start"],
                "development_end": result["development"]["end"],
                "final_test_start": result["final_test"]["start"],
                "final_test_end": result["final_test"]["end"],
                "production_start": result["production_reserved"]["start"],
                "production_end": result["production_reserved"]["end"],
                "final_test_ratio": float(final_test_ratio),
            }
        )
    else:
        split_policy.update(
            {
                "mode": "fixed_research_window",
                "development_end": DEVELOPMENT_END.isoformat(),
                "final_test_start": FINAL_TEST_START.isoformat(),
                "offline_end": OFFLINE_END.isoformat(),
                "production_start": PRODUCTION_START.isoformat(),
            }
        )

    report = {
        "created_at": datetime.now().isoformat(),
        "model": model_name,
        "variant": normalized_name,
        "selection_metric": "cross_validation_mean_MAE",
        "split_policy": split_policy,
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

    if result.get("shap_report", {}).get("source_report"):
        report["artifact_paths"].append(result["shap_report"]["source_report"])

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
    mlflow = _load_mlflow()
    if mlflow is not None:
        tracking_uri = mlflow_tracking_uri or os.getenv("MLFLOW_TRACKING_URI")
        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)
        if experiment_name:
            set_mlflow_experiment(mlflow, experiment_name)
        with mlflow.start_run(run_name=f"candidate_{model_name}") as run:
            mlflow_dataset = _build_mlflow_dataset(
                mlflow,
                feature_df,
                dataset_name=mlflow_dataset_name,
                dataset_source=mlflow_dataset_source,
            )
            if mlflow_dataset is not None:
                mlflow.log_input(mlflow_dataset, context="training")
            run_id = _get_run_id(run)
            if run_id:
                report["mlflow_run_id"] = run_id
                report["mlflow_model_uri"] = f"runs:/{run_id}/{normalized_name}"
            mlflow.set_tags(
                {
                    "model_name": model_name,
                    "model_variant": normalized_name,
                    "model_family": report["family"],
                    "selection_metric": report["selection_metric"],
                    "split_mode": report["split_policy"].get("mode", ""),
                }
            )
            mlflow.log_params(
                {
                    "cv_splits": int(n_splits),
                    "random_state": int(random_state),
                    "sequence_length": int(sequence_length),
                    "max_epochs": int(max_epochs) if model_name in RECURRENT_MODEL_NAMES else 0,
                    "batch_size": int(batch_size) if model_name in RECURRENT_MODEL_NAMES else 0,
                    "train_start_date": str(train_start_date or ""),
                    "train_end_date": str(train_end_date or ""),
                    "final_test_ratio": float(final_test_ratio),
                }
            )
            mlflow.log_metrics(
                {
                    "cv_MAE": float(report["cv_metrics"]["MAE"]["mean"]),
                    "cv_MAE_std": float(report["cv_metrics"]["MAE"]["std"]),
                    "cv_RMSE": float(report["cv_metrics"]["RMSE"]["mean"]),
                    "cv_MAPE": float(report["cv_metrics"]["MAPE"]["mean"]),
                    "test_MAE": float(report["final_test_metrics"]["MAE"]),
                    "test_RMSE": float(report["final_test_metrics"]["RMSE"]),
                    "test_MAPE": float(report["final_test_metrics"]["MAPE"]),
                }
            )
            for fold in report["folds"]:
                fold_number = int(fold["fold"])
                fold_metrics = fold["validation_metrics"]
                mlflow.log_metrics(
                    {
                        f"fold_{fold_number}_MAE": float(fold_metrics["MAE"]),
                        f"fold_{fold_number}_RMSE": float(fold_metrics["RMSE"]),
                        f"fold_{fold_number}_MAPE": float(fold_metrics["MAPE"]),
                    }
                )
            for artifact_path in (
                report_path,
                folds_path,
                predictions_path,
                result.get("shap_report", {}).get("source_report", ""),
            ):
                if os.path.exists(artifact_path):
                    mlflow.log_artifact(artifact_path)
            if model_name in RECURRENT_MODEL_NAMES:
                if os.path.exists(preprocessor_path):
                    mlflow.log_artifact(preprocessor_path)
                try:
                    import mlflow.keras
                    from tensorflow import keras

                    model = keras.models.load_model(model_path)
                    mlflow.keras.log_model(model, artifact_path=normalized_name)
                except Exception as exc:
                    print(f"MLflow keras model logging skipped: {exc}")
            else:
                try:
                    import mlflow.sklearn

                    model = joblib.load(model_path)
                    mlflow.sklearn.log_model(model, artifact_path=normalized_name)
                except Exception as exc:
                    print(f"MLflow sklearn model logging skipped: {exc}")
        if "mlflow_run_id" in report:
            _save_json(report, report_path)
    return report

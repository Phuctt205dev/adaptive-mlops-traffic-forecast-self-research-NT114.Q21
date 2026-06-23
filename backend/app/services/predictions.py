import os
import uuid
from io import BytesIO
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.db.models import (
    Dataset,
    DatasetStatus,
    ModelVersion,
    Prediction,
    Region,
    TrainingRun,
    User,
)
from backend.app.schemas.prediction import PredictionCreate
from backend.app.services.storage import get_s3_client
from src.inference import prepare_input
from src.lstm_sequences import transform_sequence_source, prepare_sequence_source
from src.preprocess import preprocess
from src.time_series_inference import TrafficHistory, predict_next_hour
from src.time_series_preprocess import prepare_hourly_time_series

NUMERIC_FEATURE_DEFAULTS = {
    "air_pollution_index": 121.0,
    "humidity": 60.0,
    "wind_speed": 5.0,
    "wind_direction": 180.0,
    "visibility_in_miles": 10.0,
    "dew_point": 10.0,
    "temperature": 20.0,
    "rain_p_h": 0.0,
    "snow_p_h": 0.0,
    "clouds_all": 30.0,
}

CATEGORICAL_FEATURE_DEFAULTS = {
    "weather_type": "Clouds",
    "weather_description": "auto_baseline",
    "is_holiday": None,
}


def _resolve_model_path(artifact_uri: str) -> str:
    if artifact_uri.startswith("file://"):
        artifact_uri = artifact_uri.removeprefix("file://")
    return artifact_uri if os.path.isabs(artifact_uri) else os.path.abspath(artifact_uri)


def _load_model(model_version: ModelVersion):
    model_path = _resolve_model_path(model_version.artifact_uri)
    if not os.path.exists(model_path):
        raise ApplicationError(
            "model_artifact_not_found",
            "Active model artifact was not found on disk.",
            500,
        )
    return joblib.load(model_path)


def _load_keras_model(model_version: ModelVersion):
    model_path = _resolve_model_path(model_version.artifact_uri)
    if not os.path.exists(model_path):
        raise ApplicationError(
            "model_artifact_not_found",
            "Active neural model artifact was not found on disk.",
            500,
        )
    from tensorflow import keras

    return keras.models.load_model(model_path)


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError("Only s3:// dataset URIs are supported.")
    bucket_key = uri.removeprefix("s3://")
    bucket, key = bucket_key.split("/", 1)
    return bucket, key


def _download_dataset_frame(dataset: Dataset) -> pd.DataFrame:
    bucket, key = _parse_s3_uri(dataset.storage_uri)
    response = get_s3_client().get_object(Bucket=bucket, Key=key)
    return pd.read_csv(BytesIO(response["Body"].read()))


def _hourly_context(dataset: Dataset) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw_frame = _download_dataset_frame(dataset)
    hourly_df, audit_df, _report = prepare_hourly_time_series(raw_frame)
    hourly_df["date_time"] = pd.to_datetime(hourly_df["date_time"], errors="raise")
    audit_df["date_time"] = pd.to_datetime(audit_df["date_time"], errors="raise")
    return hourly_df.sort_values("date_time"), audit_df.sort_values("date_time")


def _active_training_context(
    db: Session,
    model_version: ModelVersion,
) -> tuple[TrainingRun, Dataset]:
    training_run = db.get(TrainingRun, model_version.training_run_id)
    if training_run is None:
        raise ApplicationError(
            "training_run_not_found",
            "Training run for the active model was not found.",
            500,
        )
    dataset = db.get(Dataset, training_run.dataset_id)
    if dataset is None:
        raise ApplicationError(
            "dataset_not_found",
            "Dataset for the active model was not found.",
            500,
        )
    if dataset.status != DatasetStatus.VALID:
        raise ApplicationError(
            "dataset_not_valid",
            "Dataset for the active model is not valid.",
            500,
        )
    return training_run, dataset


def _production_frame(training_run: TrainingRun, dataset: Dataset) -> pd.DataFrame:
    config = training_run.configuration_json or {}
    raw_frame = _download_dataset_frame(dataset)
    raw_frame["date_time"] = pd.to_datetime(raw_frame["date_time"], errors="raise")
    train_end_date = config.get("train_end_date")
    if not train_end_date:
        raise ApplicationError(
            "production_data_not_available",
            "Training end date is missing for the active model.",
            500,
        )

    production = raw_frame[
        raw_frame["date_time"] >= pd.Timestamp(train_end_date)
    ].copy()
    if production.empty:
        raise ApplicationError(
            "production_data_not_available",
            "No production rows exist after the active model train_end_date.",
            409,
        )
    return production.sort_values("date_time")


def get_prediction_window(
    db: Session,
    region_id: uuid.UUID,
) -> dict:
    region = db.get(Region, region_id)
    if region is None or not region.is_active:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    if region.active_model_version_id is None:
        raise ApplicationError(
            "active_model_not_configured",
            "Region does not have an active model version.",
            409,
        )
    model_version = db.get(ModelVersion, region.active_model_version_id)
    if model_version is None:
        raise ApplicationError(
            "active_model_not_found",
            "Active model version was not found.",
            500,
        )
    training_run, dataset = _active_training_context(db, model_version)
    production = _production_frame(training_run, dataset)
    return {
        "region_id": region.id,
        "active_model_version_id": model_version.id,
        "model_version": model_version.version,
        "model_variant": model_version.variant,
        "dataset_id": dataset.id,
        "dataset_filename": dataset.original_filename,
        "production_start_at": production["date_time"].min().to_pydatetime(),
        "production_end_at": production["date_time"].max().to_pydatetime(),
        "available_points": int(len(production)),
    }


def _active_prediction_context(
    db: Session,
    region_id: uuid.UUID,
) -> tuple[Region, ModelVersion, TrainingRun, Dataset, pd.DataFrame]:
    region = db.get(Region, region_id)
    if region is None or not region.is_active:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    if region.active_model_version_id is None:
        raise ApplicationError(
            "active_model_not_configured",
            "Region does not have an active model version.",
            409,
        )

    model_version = db.get(ModelVersion, region.active_model_version_id)
    if model_version is None:
        raise ApplicationError(
            "active_model_not_found",
            "Active model version was not found.",
            500,
        )

    training_run, dataset = _active_training_context(db, model_version)
    production = _production_frame(training_run, dataset)
    return region, model_version, training_run, dataset, production


def _normalize_production_row(row: dict) -> dict:
    row.pop("traffic_volume", None)
    normalized_row = {}
    for key, value in row.items():
        if pd.isna(value):
            normalized_row[key] = None
        elif hasattr(value, "item"):
            normalized_row[key] = value.item()
        else:
            normalized_row[key] = value
    return normalized_row


def _snap_to_available_hour(target: datetime, production: pd.DataFrame) -> pd.Timestamp | None:
    target_ts = pd.Timestamp(target).tz_localize(None).floor("h")
    matches = production[production["date_time"] == target_ts]
    if not matches.empty:
        return target_ts

    future = production[production["date_time"] >= target_ts]
    if future.empty:
        return None
    return future.iloc[0]["date_time"]


def _build_snapshots_for_targets(
    production: pd.DataFrame,
    targets: list[datetime],
) -> list[dict]:
    snapshots = []
    seen = set()
    indexed = production.set_index("date_time", drop=False)
    for target in targets:
        available_time = _snap_to_available_hour(target, production)
        if available_time is None or available_time in seen:
            continue
        seen.add(available_time)
        row = indexed.loc[available_time].to_dict()
        row.pop("date_time", None)
        snapshot = _normalize_production_row(row)
        snapshot["date_time"] = available_time.isoformat()
        snapshots.append(snapshot)
    return snapshots


def _prepare_batch_input(snapshots: list[dict], model) -> pd.DataFrame:
    df = pd.DataFrame(snapshots)
    df["traffic_volume"] = 0
    df = preprocess(df)
    features = df.drop(["traffic_volume", "date_time"], axis=1)
    return features.reindex(columns=model.feature_names_in_, fill_value=0)


def _predict_tree_lag(
    model_version: ModelVersion,
    dataset: Dataset,
    target_time: pd.Timestamp,
) -> tuple[float, dict]:
    model = _load_model(model_version)
    hourly_df, audit_df = _hourly_context(dataset)
    merged = hourly_df.merge(
        audit_df[["date_time", "target_observed"]],
        on="date_time",
        how="left",
        validate="one_to_one",
    )
    history_rows = merged[merged["date_time"] < target_time]
    target_rows = merged[merged["date_time"] == target_time]
    if target_rows.empty:
        raise ApplicationError(
            "production_features_not_found",
            "No production feature row exists for the selected forecast time.",
            404,
        )
    history = TrafficHistory.from_dataframe(history_rows)
    target_row = target_rows.iloc[0].to_dict()
    exogenous = {
        key: value
        for key, value in target_row.items()
        if key not in {"traffic_volume", "date_time", "target_observed"}
    }
    prediction_value, feature_row = predict_next_hour(
        model,
        target_time,
        exogenous,
        history,
    )
    return prediction_value, {
        "feature_source": f"lag_history_dataset:{dataset.id}",
        "model_features": list(feature_row.columns),
    }


def _predict_neural_sequence(
    model_version: ModelVersion,
    training_run: TrainingRun,
    dataset: Dataset,
    target_time: pd.Timestamp,
) -> tuple[float, dict]:
    config = training_run.configuration_json or {}
    preprocessor_file = config.get("preprocessor_file")
    if not preprocessor_file:
        raise ApplicationError(
            "neural_preprocessor_not_found",
            "Active neural model preprocessor path is missing.",
            500,
        )
    preprocessor_path = _resolve_model_path(preprocessor_file)
    if not os.path.exists(preprocessor_path):
        raise ApplicationError(
            "neural_preprocessor_not_found",
            "Active neural model preprocessor artifact was not found.",
            500,
        )

    model = _load_keras_model(model_version)
    preprocessors = joblib.load(preprocessor_path)
    sequence_length = int(config.get("recurrent_sequence_length") or 168)
    hourly_df, audit_df = _hourly_context(dataset)
    source = prepare_sequence_source(hourly_df, audit_df).sort_values("date_time")
    source = source[source["date_time"] < target_time].reset_index(drop=True)
    if len(source) < sequence_length:
        raise ApplicationError(
            "prediction_history_insufficient",
            "Not enough history exists for the active sequence model.",
            409,
        )
    transformed = transform_sequence_source(source, preprocessors)
    sequence = transformed[-sequence_length:].reshape(
        1,
        sequence_length,
        transformed.shape[1],
    )
    scaled_prediction = model.predict(sequence, verbose=0)
    prediction = preprocessors["target_scaler"].inverse_transform(
        np.asarray(scaled_prediction).reshape(-1, 1)
    )[0, 0]
    return float(prediction), {
        "feature_source": f"sequence_history_dataset:{dataset.id}",
        "model_features": preprocessors.get("sequence_feature_names", []),
        "sequence_length": sequence_length,
    }


def _predict_value(
    model_version: ModelVersion,
    training_run: TrainingRun,
    dataset: Dataset,
    feature_snapshot: dict,
) -> tuple[float, dict]:
    config = training_run.configuration_json or {}
    model_family = config.get("model_family")
    target_time = pd.Timestamp(feature_snapshot["date_time"]).tz_localize(None)
    if model_family == "tree_autoregressive":
        return _predict_tree_lag(model_version, dataset, target_time)
    if model_family == "neural_sequence":
        return _predict_neural_sequence(
            model_version,
            training_run,
            dataset,
            target_time,
        )

    model = _load_model(model_version)
    features = prepare_input(feature_snapshot, model)
    prediction_value = float(model.predict(features)[0])
    return prediction_value, {"model_features": list(features.columns)}


def get_forecast_dashboard(
    db: Session,
    region_id: uuid.UUID,
) -> dict:
    region, model_version, training_run, dataset, production = _active_prediction_context(db, region_id)

    production_start = production["date_time"].min().to_pydatetime()
    production_end = production["date_time"].max().to_pydatetime()
    try:
        region_timezone = ZoneInfo(region.timezone or "UTC")
    except Exception:
        region_timezone = ZoneInfo("UTC")
    now = datetime.now(region_timezone).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    start = now if production_start <= now <= production_end else production_start

    hourly_targets = [start + timedelta(hours=offset) for offset in range(24)]
    daily_targets = []
    for day in range(7):
        day_start = (start + timedelta(days=day)).replace(hour=0)
        daily_targets.extend(day_start + timedelta(hours=hour) for hour in range(24))

    snapshots = _build_snapshots_for_targets(production, hourly_targets + daily_targets)
    if not snapshots:
        raise ApplicationError(
            "forecast_rows_not_found",
            "No production rows were found for dashboard forecasting.",
            404,
        )

    if (training_run.configuration_json or {}).get("model_family") in {
        "tree_autoregressive",
        "neural_sequence",
    }:
        predicted_by_time = {}
        for snapshot in snapshots:
            prediction, _feature_meta = _predict_value(
                model_version,
                training_run,
                dataset,
                snapshot,
            )
            predicted_by_time[
                pd.Timestamp(snapshot["date_time"]).to_pydatetime()
            ] = prediction
    else:
        model = _load_model(model_version)
        features = _prepare_batch_input(snapshots, model)
        predictions = model.predict(features)
        predicted_by_time = {
            pd.Timestamp(snapshot["date_time"]).to_pydatetime(): float(prediction)
            for snapshot, prediction in zip(snapshots, predictions)
        }

    hourly = [
        {"forecast_for": when, "prediction": value}
        for when, value in predicted_by_time.items()
        if when in {pd.Timestamp(target).to_pydatetime() for target in hourly_targets}
    ][:24]

    daily = []
    for day in range(7):
        day_start = (start + timedelta(days=day)).replace(hour=0)
        day_end = day_start + timedelta(days=1)
        points = [
            {"forecast_for": when, "prediction": value}
            for when, value in predicted_by_time.items()
            if day_start <= when < day_end
        ]
        if not points:
            continue
        values = [point["prediction"] for point in points]
        daily.append(
            {
                "date": day_start,
                "min_prediction": min(values),
                "max_prediction": max(values),
                "avg_prediction": sum(values) / len(values),
                "points": sorted(points, key=lambda point: point["forecast_for"]),
            }
        )

    return {
        "region_id": region.id,
        "active_model_version_id": model_version.id,
        "model_version": model_version.version,
        "model_variant": model_version.variant,
        "generated_at": datetime.now(),
        "hourly_24h": sorted(hourly, key=lambda point: point["forecast_for"]),
        "daily_7d": daily,
    }


def _build_feature_snapshot_from_production(
    training_run: TrainingRun,
    dataset: Dataset,
    payload: PredictionCreate,
) -> dict:
    production = _production_frame(training_run, dataset)
    target = pd.Timestamp(payload.forecast_for).tz_localize(None)
    production_start = production["date_time"].min()
    production_end = production["date_time"].max()
    if target < production_start or target > production_end:
        raise ApplicationError(
            "prediction_outside_production_window",
            (
                "Forecast time must be inside the active model production window "
                f"from {production_start.isoformat()} to {production_end.isoformat()}."
            ),
            400,
        )

    matches = production[production["date_time"] == target]
    if matches.empty:
        raise ApplicationError(
            "production_features_not_found",
            "No production feature row exists for the selected forecast time.",
            404,
        )
    row = matches.iloc[0].to_dict()
    row.pop("traffic_volume", None)
    row.pop("date_time", None)
    normalized_row = {}
    for key, value in row.items():
        if pd.isna(value):
            normalized_row[key] = None
        elif hasattr(value, "item"):
            normalized_row[key] = value.item()
        else:
            normalized_row[key] = value

    return {
        **normalized_row,
        "date_time": target.isoformat(),
        "feature_source": f"production_dataset:{dataset.id}",
        "production_window": {
            "start": production_start.isoformat(),
            "end": production_end.isoformat(),
        },
    }


def predict_for_region(
    db: Session,
    region_id: uuid.UUID,
    payload: PredictionCreate,
    user: User,
) -> tuple[Prediction, ModelVersion]:
    region = db.get(Region, region_id)
    if region is None or not region.is_active:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    if region.active_model_version_id is None:
        raise ApplicationError(
            "active_model_not_configured",
            "Region does not have an active model version.",
            409,
        )

    model_version = db.get(ModelVersion, region.active_model_version_id)
    if model_version is None:
        raise ApplicationError(
            "active_model_not_found",
            "Active model version was not found.",
            500,
        )

    training_run, dataset = _active_training_context(db, model_version)
    feature_snapshot = _build_feature_snapshot_from_production(
        training_run,
        dataset,
        payload,
    )
    prediction_value, feature_meta = _predict_value(
        model_version,
        training_run,
        dataset,
        feature_snapshot,
    )

    prediction = Prediction(
        user_id=user.id,
        region_id=region.id,
        model_version_id=model_version.id,
        forecast_for=payload.forecast_for,
        forecast_mode=payload.forecast_mode,
        prediction=prediction_value,
        feature_snapshot_json={
            **feature_snapshot,
            **feature_meta,
        },
    )
    db.add(prediction)
    db.commit()
    db.refresh(prediction)
    return prediction, model_version

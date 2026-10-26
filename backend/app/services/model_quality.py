"""Short, read-only replay reports for the active model. Never fit a model here."""

import copy
import json
import math
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

from backend.app.core.errors import ApplicationError
from backend.app.db.models import ModelVersion, Region
from backend.app.services import predictions
from backend.app.services.datasets import REQUIRED_COLUMNS
from src.time_series_inference import InsufficientHistoryError


MAX_HOURS = 168
CACHE_SECONDS = 60
_reports = OrderedDict()
_evaluation_lock = threading.Lock()


def _local_time(value, timezone):
    """Naive CSV times are region wall times; aware times represent instants."""
    result = pd.Timestamp(value)
    if pd.isna(result):
        raise ValueError("Missing timestamp")
    if result.tzinfo is not None:
        result = result.tz_convert(timezone).tz_localize(None)
    return result


def _flag(value):
    if value is None or pd.isna(value):
        return None
    return {"true": True, "false": False, "1": True, "0": False,
            "1.0": True, "0.0": False}.get(str(value).strip().lower())


def label_source(row):
    """A filled target is never an observation. Missing audit means unknown."""
    raw_observed = row.get("target_observed")
    raw_imputed = row.get("target_is_imputed")
    observed = _flag(raw_observed)
    imputed = _flag(raw_imputed)
    if observed is False or imputed is True:
        return "synthetic"
    for raw, parsed in ((raw_observed, observed), (raw_imputed, imputed)):
        if raw is not None and not pd.isna(raw) and parsed is None:
            return "unknown"
    if observed is True:
        return "observed"
    return "unknown"


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _predict_points(model_version, training_run, dataset, rows):
    """Use serving preprocessing for each supported family, with one model load."""
    if rows.empty:
        return {}
    family = (training_run.configuration_json or {}).get("model_family")
    # Audit columns must not be model inputs or affect preprocess.dropna().
    snapshots = rows[REQUIRED_COLUMNS].drop(columns="traffic_volume").to_dict("records")
    for snapshot in snapshots:
        snapshot["date_time"] = pd.Timestamp(snapshot["date_time"]).isoformat()

    if family == "neural_sequence":
        return predictions._predict_neural_sequence_batch(
            model_version, training_run, dataset, snapshots,
        )

    if family not in {None, "legacy_sklearn", "tree_autoregressive"}:
        raise ApplicationError("quality_model_unsupported", "This model family does not support replay.", 409)
    model = predictions._load_model(model_version)
    if family == "tree_autoregressive":
        # Walk history once. A target enters history only AFTER its prediction.
        hourly, audit = predictions._hourly_context(dataset)
        source = hourly.merge(audit[["date_time", "target_observed"]], on="date_time",
                              validate="one_to_one").sort_values("date_time")
        first = pd.Timestamp(rows["date_time"].min())
        last = pd.Timestamp(rows["date_time"].max())
        history = predictions.TrafficHistory.from_dataframe(source[source.date_time < first])
        wanted = set(rows["date_time"])
        results = {}
        for row in source[(source.date_time >= first) & (source.date_time <= last)].to_dict("records"):
            when = pd.Timestamp(row["date_time"])
            if when in wanted:
                exogenous = {key: row[key] for key in REQUIRED_COLUMNS
                             if key not in {"date_time", "traffic_volume"}}
                try:
                    value, _features = predictions.predict_next_hour(model, when, exogenous, history)
                    results[when.to_pydatetime()] = float(value)
                except InsufficientHistoryError:
                    pass  # Keep a gap until enough past hours exist.
            history.append(when, row["traffic_volume"], row["target_observed"])
        return results

    # Match individual serving requests. Encoding a whole window at once can
    # change dummy-category baselines when the selected window changes.
    features = pd.concat([predictions.prepare_input(snapshot, model) for snapshot in snapshots],
                         ignore_index=True)
    if len(features) != len(snapshots):
        raise ApplicationError("quality_features_missing", "Some replay rows have incomplete model inputs.", 409)
    values = np.asarray(model.predict(features)).reshape(-1)
    if len(values) != len(rows):
        raise ApplicationError("quality_prediction_mismatch", "Model returned an unexpected number of predictions.", 409)
    return {pd.Timestamp(when).to_pydatetime(): float(value)
            for when, value in zip(rows["date_time"], values)}


def _recent_start(timezone, now):
    return _local_time(now, timezone).floor("h") - pd.Timedelta(hours=24)


def _build_report(region, model, training, dataset, frame, mode, start_at, hours,
                  *, window="replay", now=None):
    now = now if now is not None else datetime.now(UTC)
    try:
        timezone = ZoneInfo(region.timezone)
        training_end = _local_time((training.configuration_json or {})["train_end_date"], timezone)
        frame = frame.copy()
        frame["date_time"] = frame["date_time"].map(lambda value: _local_time(value, timezone))
    except (KeyError, ValueError, TypeError, ZoneInfoNotFoundError) as error:
        raise ApplicationError("quality_timeline_invalid", "Dataset timezone or training boundary is invalid.", 409) from error
    if frame["date_time"].duplicated().any():
        raise ApplicationError("quality_duplicate_times", "Dataset contains ambiguous or duplicate local hours.", 409)
    production = frame[frame.date_time >= training_end].sort_values("date_time")
    if production.empty and window == "replay":
        raise ApplicationError("quality_no_holdout", "No dataset rows exist after the training boundary.", 409)
    try:
        if window == "recent":
            start = _recent_start(timezone, now)
        else:
            start = _local_time(start_at, timezone) if start_at else production.date_time.min()
    except (ValueError, TypeError) as error:
        raise ApplicationError("quality_start_invalid", "Replay start time is invalid.", 422) from error
    if start != start.floor("h"):
        raise ApplicationError("quality_start_invalid", "Select a whole hour for replay.", 422)
    if window == "replay" and (start < training_end or start < production.date_time.min() or start > production.date_time.max()):
        raise ApplicationError("quality_outside_window", "Select a time inside the available post-training window.", 409)

    timeline = pd.date_range(start, periods=hours, freq="h")
    rows = production[production.date_time.isin(timeline)].copy()
    if not set(REQUIRED_COLUMNS).issubset(rows.columns):
        raise ApplicationError("quality_schema_invalid", "Dataset is missing replay input columns.", 409)
    try:
        predicted = _predict_points(model, training, dataset, rows)
    except predictions.ApplicationError as error:
        if error.code != "prediction_history_insufficient":
            raise
        predicted = {}
    by_time = {pd.Timestamp(row["date_time"]): row for row in rows.to_dict("records")}
    points = []
    counts = {"observed": 0, "synthetic": 0, "unknown": 0}
    for when in timeline:
        row = by_time.get(when)
        source = label_source(row) if row is not None else "missing"
        if row is not None:
            counts[source] += 1
        reference = _number(row.get("traffic_volume")) if row is not None else None
        if reference is not None and reference < 0:
            reference = None
        if mode == "observed" and source != "observed":
            reference = None
        value = _number(predicted.get(when.to_pydatetime())) if row is not None else None
        error = _number(abs(reference - value)) if reference is not None and value is not None else None
        points.append({"forecast_for": when.to_pydatetime(), "reference": reference,
                       "prediction": value, "absolute_error": error, "label_source": source})

    errors = [point["absolute_error"] for point in points if point["absolute_error"] is not None]
    warnings = [
        "Historical replay using dataset inputs; this is not live traffic monitoring or a multi-day-ahead forecast.",
        "Observed labels are declared by target_observed in this dataset, not verified against a live camera.",
    ]
    if mode == "reference":
        warnings.append("Reference MAE/RMSE measure agreement with the dataset, including simulated or unverified values; they do not establish real-world accuracy.")
    if counts["unknown"]:
        warnings.append("Some rows have no label provenance. They are excluded from observed-label metrics.")
    if window == "recent":
        warnings.append("This window covers the last 24 completed hours in region time; missing data never shifts it to another date.")
        if start < training_end:
            warnings.append("Hours before the active model's training boundary are excluded to avoid scoring on training data.")
    if not errors:
        warnings.append("No eligible prediction/reference pairs exist in this window. Quality metrics are unavailable.")
    if any(point["prediction"] is None for point in points):
        warnings.append("Some hours have no prediction (missing rows or insufficient model history). They are excluded from metrics.")

    def as_iso(value):
        # Preserve dataset wall time and its region offset, independent of browser timezone.
        try:
            return pd.Timestamp(value).tz_localize(timezone).isoformat()
        except ValueError as error:
            raise ApplicationError("quality_timeline_invalid", "Replay includes an ambiguous or nonexistent local hour.", 409) from error

    for point in points:
        point["forecast_for"] = as_iso(point["forecast_for"])
    return {
        "region_id": region.id, "active_model_version_id": model.id,
        "model_version": model.version, "model_variant": model.variant,
        "dataset_id": dataset.id, "dataset_filename": dataset.original_filename,
        "timezone": region.timezone, "mode": mode, "window": window,
        "evaluation_policy": "post_training_one_step_replay_with_dataset_inputs",
        "generated_at": pd.Timestamp(now).isoformat(),
        "start_at": as_iso(start), "end_at": as_iso(timeline[-1]),
        "window_end_at": as_iso(timeline[-1] + pd.Timedelta(hours=1)),
        "available_start_at": as_iso(production.date_time.min()) if not production.empty else None,
        "available_end_at": as_iso(production.date_time.max()) if not production.empty else None,
        "training_end_at": as_iso(training_end),
        "metrics": {"mae": math.fsum(error / len(errors) for error in errors) if errors else None,
                    "rmse": math.hypot(*(error / math.sqrt(len(errors)) for error in errors)) if errors else None},
        "coverage": {"requested_points": hours, "dataset_points": len(rows),
                     "predicted_points": sum(point["prediction"] is not None for point in points),
                     "paired_points": len(errors), "observed_labels": counts["observed"],
                     "synthetic_labels": counts["synthetic"], "unknown_labels": counts["unknown"]},
        "warnings": warnings, "points": points,
    }


def get_model_quality(db, region_id, *, mode="observed", start_at=None, hours=24,
                      window="recent", expected_model_id=None, serving_runtime=None):
    if window not in {"recent", "replay"} or mode not in {"reference", "observed"} or not 1 <= hours <= MAX_HOURS:
        raise ApplicationError("quality_request_invalid", "Select a valid mode and between 1 and 168 hours.", 422)
    if window == "recent" and (mode != "observed" or start_at is not None or hours != 24):
        raise ApplicationError("quality_request_invalid", "Recent quality uses observed labels and the last 24 completed hours automatically.", 422)
    region = db.get(Region, region_id)
    if region is None or not region.is_active:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    model = db.get(ModelVersion, region.active_model_version_id) if region.active_model_version_id else None
    if model is None:
        raise ApplicationError("active_model_not_configured", "Region does not have an active model version.", 409)
    if expected_model_id is not None and model.id != expected_model_id:
        raise ApplicationError("quality_active_model_changed", "Active model changed. Refresh the region before evaluating.", 409)
    training, dataset = predictions._active_training_context(db, model)
    now = datetime.now(UTC)
    try:
        window_key = str(_recent_start(ZoneInfo(region.timezone), now)) if window == "recent" else str(start_at)
    except (ValueError, TypeError, ZoneInfoNotFoundError) as error:
        raise ApplicationError("quality_timeline_invalid", "Region timezone is invalid.", 409) from error
    key = (str(region.id), str(model.id), model.artifact_uri, str(dataset.id), dataset.sha256,
           region.timezone, json.dumps(training.configuration_json or {}, sort_keys=True),
           mode, window, window_key, hours)

    # Reject competing reports instead of building an unbounded evaluation queue.
    if not _evaluation_lock.acquire(blocking=False):
        raise ApplicationError("quality_busy", "A quality report is already being calculated. Try again shortly.", 503)
    runtime_acquired = False
    try:
        cached = _reports.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return copy.deepcopy(cached[1])
        # The deployed runtime shares this lock with normal model inference.
        if serving_runtime is not None:
            runtime_acquired = serving_runtime.lock.acquire(blocking=False)
            if not runtime_acquired:
                raise ApplicationError("quality_busy", "Prediction service is busy. Try the quality report again shortly.", 503)
        report = _build_report(region, model, training, dataset,
                               predictions._download_dataset_frame(dataset), mode, start_at, hours,
                               window=window, now=now)
        _reports[key] = (time.monotonic(), copy.deepcopy(report))
        _reports.move_to_end(key)
        while len(_reports) > 4:
            _reports.popitem(last=False)
        return report
    finally:
        if runtime_acquired:
            serving_runtime.lock.release()
        _evaluation_lock.release()

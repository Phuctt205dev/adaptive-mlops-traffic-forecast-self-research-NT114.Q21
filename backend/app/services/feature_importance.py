"""Read saved SHAP summaries and average normalized reports. Never fit/explain."""

import hashlib
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from sqlalchemy import select

from backend.app.core.errors import ApplicationError
from backend.app.db.models import Dataset, ModelVersion, Region, TrainingRun
from backend.app.services import storage


MODEL_ROOT = Path("models").resolve()
MAX_REPORT_BYTES = 512 * 1024
LABELS = {
    "hour": "Time of day", "hour_sin": "Time of day", "hour_cos": "Time of day",
    "day": "Day of week", "is_weekend": "Day of week", "dayofweek": "Day of week",
    "month": "Month", "is_holiday": "Holiday", "temperature": "Temperature",
    "humidity": "Humidity", "air_pollution_index": "Air pollution",
    "wind_speed": "Wind speed", "wind_direction": "Wind direction",
    "visibility_in_miles": "Visibility", "dew_point": "Dew point",
    "rain_p_h": "Rainfall", "snow_p_h": "Snowfall", "clouds_all": "Cloud cover",
    "day_of_week_sin": "Day of week", "day_of_week_cos": "Day of week",
    "month_sin": "Month", "month_cos": "Month", "is_holiday_binary": "Holiday",
    "traffic_history_value": "Past traffic volume",
    "traffic_history_observed": "Past traffic availability",
}


def tree_name(value):
    return str(value).lower().replace("_", "").replace("-", "")


def report_bytes(reference):
    if not isinstance(reference, str) or reference.startswith(("s3:", "http:", "https:")):
        raise ValueError("Invalid report reference")
    original = Path(reference.removeprefix("file://"))
    if any(part.is_symlink() for part in (original, *original.parents)):
        raise ValueError("Symlink report references are not supported")
    path = original.resolve()
    if not path.is_relative_to(MODEL_ROOT) or path == MODEL_ROOT:
        raise ValueError("Report must be under the model artifact root")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Symlink report references are not supported")
    if path.is_file():
        if not 0 < path.stat().st_size <= MAX_REPORT_BYTES:
            raise ValueError("Report size exceeds its budget")
        with path.open("rb") as stream:
            return stream.read(MAX_REPORT_BYTES + 1)
    bucket = os.environ.get("TRAFFIC_API_RUNTIME_BUCKET")
    if not bucket:
        raise FileNotFoundError("Report storage is unavailable")
    response = storage.get_s3_client().get_object(Bucket=bucket,
        Key="models/" + path.relative_to(MODEL_ROOT).as_posix())
    with response["Body"] as body:
        if not 0 < response["ContentLength"] <= MAX_REPORT_BYTES:
            raise ValueError("Report size exceeds its budget")
        return body.read(MAX_REPORT_BYTES + 1)


def grouped_features(importance):
    if not isinstance(importance, list) or not 1 <= len(importance) <= 1000:
        raise ValueError("Invalid SHAP importance rows")
    groups, seen = {}, set()
    for row in importance:
        name = row["feature"]
        score = float(row["mean_abs_shap"])
        if not isinstance(name, str) or not name or len(name) > 200 or name in seen or not math.isfinite(score) or score < 0:
            raise ValueError("Invalid SHAP feature or score")
        seen.add(name)
        if name.startswith("weather_type_"):
            label = "Weather conditions"
        elif name.startswith("weather_description_"):
            label = "Weather description"
        elif name.startswith("is_holiday_"):
            label = "Holiday"
        else:
            label = LABELS.get(name, name.replace("_", " ").capitalize())
        group = groups.setdefault(label, {"label": label, "mean_abs_shap": 0.0, "features": []})
        group["mean_abs_shap"] += score
        group["features"].append(name)
    total = sum(group["mean_abs_shap"] for group in groups.values())
    if not math.isfinite(total):
        raise ValueError("SHAP importance overflow")
    for group in groups.values():
        group["share_percent"] = 100 * group["mean_abs_shap"] / total if total else 0.0
    return sorted(groups.values(), key=lambda group: (-group["mean_abs_shap"], group["label"]))


def get_active_feature_importance(db, region_id, expected_model_id=None):
    region = db.get(Region, region_id)
    if region is None:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    if expected_model_id and expected_model_id != region.active_model_version_id:
        raise ApplicationError("active_model_changed", "The active model changed. Refresh this page.", 409)
    model = db.get(ModelVersion, region.active_model_version_id) if region.active_model_version_id else None
    if model is None or model.region_id != region.id:
        raise ApplicationError("active_model_not_found", "This region has no active model.", 409)
    run = db.get(TrainingRun, model.training_run_id)
    dataset = db.get(Dataset, run.dataset_id) if run and run.region_id == region.id else None
    if dataset is None or dataset.region_id != region.id:
        raise ApplicationError("model_dataset_not_found", "The model dataset was not found.", 409)
    return model_feature_importance(region, model, run, dataset)


def model_feature_importance(region, model, run, dataset):
    """Use the same ownership, digest and schema checks for every saved version."""
    if (model.region_id != region.id or run is None or run.id != model.training_run_id
            or run.region_id != region.id or dataset is None or dataset.id != run.dataset_id
            or dataset.region_id != region.id):
        raise ApplicationError("model_dataset_not_found", "The model dataset was not found.", 409)
    result = {"region_id": region.id, "active_model_version_id": model.id,
              "model_version": model.version, "model_variant": model.variant,
              "dataset_filename": dataset.original_filename, "status": "pending",
              "message": "A SHAP report has not been created for this model yet.",
              "generated_at": None, "sample_count": 0, "transformed_feature_count": 0, "features": []}
    neural = tree_name(model.variant) in {"lstm", "gru"}
    if not neural and not tree_name(model.variant).startswith(("randomforest", "xgboost", "lightgbm")):
        return {**result, "status": "unsupported", "message": "Feature importance is unavailable for this model type."}
    configuration = run.configuration_json or {}
    if not isinstance(configuration, dict):
        raise ApplicationError("shap_report_invalid", "The saved report does not match this model.", 409)
    metadata = configuration.get("shap_report")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ApplicationError("shap_report_invalid", "The saved report does not match this model.", 409)
    if metadata.get("status") != "completed":
        if metadata.get("status") in {"failed", "unavailable", "disabled"}:
            return {**result, "status": "unavailable", "message": "No completed SHAP report is available for this model."}
        return result
    try:
        raw = report_bytes(metadata["source_report"])
        if len(raw) > MAX_REPORT_BYTES or hashlib.sha256(raw).hexdigest() != metadata["report_sha256"]:
            raise ValueError("Report digest differs")
        report = json.loads(raw)
        if not isinstance(report, dict) or not isinstance(report.get("created_at"), str):
            raise ValueError("Invalid report timestamp")
        datetime.fromisoformat(report["created_at"].replace("Z", "+00:00"))
        context = report["context"]
        if (not isinstance(context, dict) or report["schema_version"] != 1 or report["status"] != "completed"
                or context.get("region_id") != str(region.id) or context.get("dataset_id") != str(dataset.id)
                or report["model_sha256"] != metadata["model_sha256"]
                or tree_name(report["model_name"]) != tree_name(model.variant)
                or report["explainer"] != ("GradientExplainer" if neural else "TreeExplainer")
                or report.get("final_test_used") is not False
                or type(report["sample_count"]) is not int or not 1 <= report["sample_count"] <= 128):
            raise ValueError("Report belongs to another model/dataset")
        if neural:
            preprocessor_sha = metadata.get("preprocessor_sha256")
            length = report.get("sequence_length")
            if (not isinstance(preprocessor_sha, str) or not re.fullmatch("[0-9a-f]{64}", preprocessor_sha)
                    or context.get("preprocessor_sha256") != preprocessor_sha
                    or context.get("training_run_id") != str(run.id)
                    or not (run.configuration_json or {}).get("preprocessor_file")
                    or report.get("approximate") is not True
                    or report.get("aggregation") != "mean_samples_sum_timesteps_abs_shap"
                    or type(length) is not int or not 1 <= length <= 10000
                    or (run.configuration_json or {}).get("recurrent_sequence_length", length) != length
                    or report.get("output_units") != "traffic_volume"):
                raise ValueError("Neural report does not match the sequence/preprocessor")
        features = grouped_features(report["importance"])
        return {**result, "status": "ready", "message": None, "features": features,
                "sample_count": report["sample_count"], "generated_at": report["created_at"],
                "transformed_feature_count": len(report["importance"]),
                "explanation_method": report["explainer"],
                "sequence_length": report["sequence_length"] if neural else None}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ApplicationError("shap_report_invalid", "The saved report does not match this model.", 409) from None
    except Exception:
        raise ApplicationError("shap_report_unavailable", "The report is temporarily unavailable. Please refresh.", 503) from None


def saved_model_history(db, region, active_report):
    # One streamed join; each object read remains bounded to MAX_REPORT_BYTES.
    # No version cap or model/status filter: all stored versions in this region count.
    statement = (select(ModelVersion, TrainingRun, Dataset)
        .outerjoin(TrainingRun, TrainingRun.id == ModelVersion.training_run_id)
        .outerjoin(Dataset, Dataset.id == TrainingRun.dataset_id)
        .where(ModelVersion.region_id == region.id).order_by(ModelVersion.id)
        .execution_options(yield_per=100))
    summary = {"status": "pending", "total_models": 0, "used_reports": 0,
               "excluded_reports": {key: 0 for key in ("missing", "unavailable", "unsupported", "invalid", "zero")},
               "dataset_count": 0, "features": [], "models": []}
    groups, datasets = {}, set()
    for model, run, dataset in db.execute(statement):
        summary["total_models"] += 1
        item = {"model_version_id": model.id, "model_version": model.version,
                "model_variant": model.variant,
                "dataset_filename": dataset.original_filename if dataset and dataset.region_id == region.id else None}
        try:
            report = (active_report if model.id == region.active_model_version_id and active_report
                      else model_feature_importance(region, model, run, dataset))
            status = report["status"]
            if status != "ready":
                reason = report.get("exclusion_reason") or {"pending": "missing", "unavailable": "unavailable",
                                                          "unsupported": "unsupported"}[status]
            elif not any(row["mean_abs_shap"] > 0 for row in report["features"]):
                reason = "zero"
            else:
                reason = None
        except ApplicationError as error:
            reason = "unavailable" if error.status_code == 503 else "invalid"
        if reason:
            summary["excluded_reports"][reason] += 1
            summary["models"].append({**item, "status": reason})
            continue
        summary["used_reports"] += 1
        datasets.add(dataset.id)
        summary["models"].append({**item, "status": "included"})
        for row in report["features"]:
            group = groups.setdefault(row["label"], {"label": row["label"], "share_percent": 0.0,
                                                     "features": set(), "reports_present": 0})
            group["share_percent"] += row["share_percent"]
            group["features"].update(row["features"])
            group["reports_present"] += 1
    used = summary["used_reports"]
    if used:
        summary["status"] = "ready"
        summary["features"] = sorted([
            {**group, "features": sorted(group["features"]), "share_percent": group["share_percent"] / used}
            for group in groups.values()], key=lambda row: (-row["share_percent"], row["label"]))
    summary["dataset_count"] = len(datasets)
    return summary


def get_feature_importance(db, region_id, expected_model_id=None):
    region = db.get(Region, region_id)
    if region is None:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    if expected_model_id and expected_model_id != region.active_model_version_id:
        raise ApplicationError("active_model_changed", "The active model changed. Refresh this page.", 409)
    empty = {"region_id": region.id, "active_model_version_id": region.active_model_version_id,
             "model_version": None, "model_variant": None, "dataset_filename": None,
             "status": "pending", "message": "This region has no active model.",
             "generated_at": None, "sample_count": 0, "transformed_feature_count": 0, "features": []}
    active = None
    if region.active_model_version_id:
        try:
            active = get_active_feature_importance(db, region.id, expected_model_id)
        except ApplicationError as error:
            # A broken active report must not hide valid historical reports.
            active = {**empty, "status": "unavailable", "message": error.message,
                      "exclusion_reason": "unavailable" if error.status_code == 503 else "invalid"}
    history = saved_model_history(db, region, active)
    return {**(active or empty), "history": history}

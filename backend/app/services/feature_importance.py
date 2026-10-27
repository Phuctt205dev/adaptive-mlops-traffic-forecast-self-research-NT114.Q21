"""Read bounded, saved SHAP summaries for the active tree model. Never fit/explain."""

import hashlib
import json
import math
import os
from pathlib import Path

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


def get_feature_importance(db, region_id, expected_model_id=None):
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
    result = {"region_id": region.id, "active_model_version_id": model.id,
              "model_version": model.version, "model_variant": model.variant,
              "dataset_filename": dataset.original_filename, "status": "pending",
              "message": "A SHAP report has not been created for this model yet.",
              "generated_at": None, "sample_count": 0, "transformed_feature_count": 0, "features": []}
    if not tree_name(model.variant).startswith(("randomforest", "xgboost", "lightgbm")):
        return {**result, "status": "unsupported", "message": "Feature importance for LSTM and GRU will be added later."}
    metadata = (run.configuration_json or {}).get("shap_report") or {}
    if metadata.get("status") != "completed":
        if metadata.get("status") in {"failed", "unavailable", "disabled"}:
            return {**result, "status": "unavailable", "message": "No completed SHAP report is available for this model."}
        return result
    try:
        raw = report_bytes(metadata["source_report"])
        if len(raw) > MAX_REPORT_BYTES or hashlib.sha256(raw).hexdigest() != metadata["report_sha256"]:
            raise ValueError("Report digest differs")
        report = json.loads(raw)
        context = report["context"]
        if (not isinstance(context, dict) or report["schema_version"] != 1 or report["status"] != "completed"
                or context.get("region_id") != str(region.id) or context.get("dataset_id") != str(dataset.id)
                or report["model_sha256"] != metadata["model_sha256"]
                or tree_name(report["model_name"]) != tree_name(model.variant)
                or report["explainer"] != "TreeExplainer" or report.get("final_test_used") is not False
                or type(report["sample_count"]) is not int or not 1 <= report["sample_count"] <= 128):
            raise ValueError("Report belongs to another model/dataset")
        features = grouped_features(report["importance"])
        return {**result, "status": "ready", "message": None, "features": features,
                "sample_count": report["sample_count"], "generated_at": report["created_at"],
                "transformed_feature_count": len(report["importance"])}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ApplicationError("shap_report_invalid", "The saved report does not match this model.", 409) from None
    except Exception:
        raise ApplicationError("shap_report_unavailable", "The report is temporarily unavailable. Please refresh.", 503) from None

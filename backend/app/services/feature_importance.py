"""Read saved SHAP summaries and average normalized reports. Never fit/explain."""

import hashlib
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from backend.app.core.errors import ApplicationError
from backend.app.db.models import Dataset, ModelVersion, Region, TrainingRun
from backend.app.services import storage


MODEL_ROOT = Path("models").resolve()
MAX_REPORT_BYTES = 512 * 1024
MODEL_NAMES = ("RandomForest", "XGBoost", "LightGBM", "LSTM", "GRU")
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
        group["share_percent"] = 100 * (group["mean_abs_shap"] / total) if total else 0.0
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


def model_feature_importance(region, model, run, dataset, expected_run_id=None):
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
                or (expected_run_id is not None and context.get("training_run_id") != str(expected_run_id))
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
                "input_features": [row["feature"] for row in report["importance"]],
                "sequence_length": report["sequence_length"] if neural else None}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ApplicationError("shap_report_invalid", "The saved report does not match this model.", 409) from None
    except Exception:
        raise ApplicationError("shap_report_unavailable", "The report is temporarily unavailable. Please refresh.", 503) from None


def active_run_reports(region, model, run, dataset, active):
    """One report per algorithm in this activated run; never borrow other versions."""
    configuration = run.configuration_json if isinstance(run.configuration_json, dict) else {}
    collection = configuration.get("shap_reports") or {}
    if not isinstance(collection, dict):
        collection = {}
    reports, groups = [], {}
    used = 0
    for name in MODEL_NAMES:
        metadata = collection.get(name)
        fallback = tree_name(name) == tree_name(model.variant) and metadata is None
        result = {"model_name": name, "status": "pending", "message": "No saved report for this model in the activated training run.",
                  "features": [], "sample_count": 0, "generated_at": None, "sequence_length": None,
                  "explanation_method": None, "input_features": [], "is_serving": tree_name(name) == tree_name(model.variant)}
        try:
            if fallback:
                report = active
            elif metadata is not None:
                if not isinstance(metadata, dict):
                    raise ApplicationError("shap_report_invalid", "The saved report is invalid.", 409)
                config = {**configuration, "shap_report": metadata,
                          "preprocessor_file": metadata.get("preprocessor_file"),
                          "recurrent_sequence_length": metadata.get("sequence_length")}
                context_run = SimpleNamespace(id=run.id, region_id=run.region_id,
                    dataset_id=run.dataset_id, configuration_json=config)
                context_model = SimpleNamespace(id=model.id, region_id=model.region_id, training_run_id=run.id,
                    version=model.version, variant=name)
                report = model_feature_importance(region, context_model, context_run, dataset, expected_run_id=run.id)
            else:
                report = None
            if report:
                for key in ("status", "message", "features", "sample_count", "generated_at", "sequence_length", "explanation_method", "input_features"):
                    if key in report:
                        result[key] = report[key]
        except ApplicationError as error:
            result.update(status="unavailable", message=error.message)
        reports.append(result)
        if result["status"] != "ready" or not any(row["mean_abs_shap"] > 0 for row in result["features"]):
            continue
        used += 1
        for row in result["features"]:
            group = groups.setdefault(row["label"], {"label": row["label"], "share_percent": 0.,
                "features": set(), "reports_present": 0})
            group["share_percent"] += row["share_percent"]
            group["features"].update(row["features"])
            group["reports_present"] += 1
    # An incomplete set must never be presented as the average of all five.
    complete = used == len(MODEL_NAMES)
    average = {"status": "ready" if complete else "incomplete", "used_reports": used,
               "total_models": len(MODEL_NAMES), "features": []}
    if complete:
        average["features"] = sorted([{**row, "features": sorted(row["features"]),
            "share_percent": row["share_percent"] / used} for row in groups.values()],
            key=lambda row: (-row["share_percent"], row["label"]))
    return reports, average


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
            # A broken serving report must not hide the other algorithms.
            active = {**empty, "status": "unavailable", "message": error.message,
                      "exclusion_reason": "unavailable" if error.status_code == 503 else "invalid"}
    reports, average = [], {"status": "incomplete", "used_reports": 0, "total_models": 5, "features": []}
    model = db.get(ModelVersion, region.active_model_version_id) if region.active_model_version_id else None
    if model and model.region_id == region.id:
        run = db.get(TrainingRun, model.training_run_id)
        dataset = db.get(Dataset, run.dataset_id) if run and run.region_id == region.id else None
        if dataset and dataset.region_id == region.id:
            reports, average = active_run_reports(region, model, run, dataset, active)
            if active:
                active.update(model_version=model.version, model_variant=model.variant,
                              dataset_filename=dataset.original_filename)
    return {**(active or empty), "model_reports": reports, "model_average": average}


def active_model_feature_usage(db, region):
    """Input membership, including zero-SHAP inputs; not a drift trigger/filter."""
    result = {"model_version_id": str(region.active_model_version_id) if region.active_model_version_id else None,
              "model_version": None, "model_variant": None, "input_features": None}
    model = db.get(ModelVersion, region.active_model_version_id) if region.active_model_version_id else None
    if not model or model.region_id != region.id:
        return result
    result.update(model_version=model.version, model_variant=model.variant)
    run = db.get(TrainingRun, model.training_run_id)
    if not run or run.region_id != region.id:
        return result
    names = (run.configuration_json if isinstance(run.configuration_json, dict) else {}).get("model_input_features")
    if not (isinstance(names, list) and 0 < len(names) <= 1000 and
            all(isinstance(name, str) and 0 < len(name) <= 200 for name in names)):
        try:
            names = get_active_feature_importance(db, region.id).get("input_features")
        except ApplicationError:
            names = None
    result["input_features"] = names or None
    return result


def raw_feature_is_used(name, inputs):
    if inputs is None:
        return None
    if name == "is_holiday":
        return any(value == "is_holiday" or value.startswith("is_holiday_") for value in inputs)
    if name in {"weather_type", "weather_description"}:
        return any(value == name or value.startswith(name + "_") for value in inputs)
    return name in inputs

"""Build trusted retrain choices. Never fit, compute SHAP, or change active models."""
import math

import pandas as pd

from backend.app.core.config import get_settings
from backend.app.core.errors import ApplicationError
from backend.app.services import model_quality
from src.preprocess import preprocess


def quality_decision(current, baseline, *, threshold=.15, min_hours=24, min_coverage=.8):
    for report in (current, baseline):
        coverage = report.get("coverage", {})
        paired, requested = coverage.get("paired_points", 0), coverage.get("requested_points", 0)
        if paired < min_hours or not requested or paired / requested < min_coverage:
            return {"status": "unknown", "reason": "quality_coverage_insufficient"}
        mae = report.get("metrics", {}).get("mae")
        if not isinstance(mae, (float, int)) or not math.isfinite(mae) or mae < 0:
            return {"status": "unknown", "reason": "quality_mae_unavailable"}
    if (current.get("active_model_version_id") != baseline.get("active_model_version_id")
            or current.get("dataset_id") != baseline.get("dataset_id")):
        return {"status": "unknown", "reason": "quality_context_changed"}
    previous, recent = baseline["metrics"]["mae"], current["metrics"]["mae"]
    # A zero-error baseline can only be maintained, never used as a divisor.
    limit = previous * (1 + threshold)
    degraded = recent > limit and not math.isclose(recent, limit, rel_tol=1e-12, abs_tol=0.)
    return {"status": "degraded" if degraded else "acceptable", "reason": "paired_replay_windows",
            "baseline_mae": previous, "current_mae": recent, "relative_threshold": threshold,
            "quality_window_end": str(current["end_at"]), "baseline_window_end": str(baseline["end_at"]),
            "reference_source": "dataset_traffic_volume", "evaluation": "same_active_model_replay"}


def prepare_retrain_selection(db, region, model, training, dataset, configuration, frame, *, serving_runtime=None):
    settings = get_settings()
    profiles = (training.configuration_json or {}).get("feature_profiles", {})
    decision = {"status": "unknown", "reason": "no_validated_feature_profile"}
    if any(profile.get("status") == "validated" for profile in profiles.values() if isinstance(profile, dict)):
        try:
            # Quality policy must not peek at the candidate's reserved holdout.
            processed = preprocess(frame.copy())
            times = processed.loc[(processed.date_time >= pd.Timestamp(configuration["train_start_date"]))
                                  & (processed.date_time < pd.Timestamp(configuration["train_end_date"])), "date_time"].sort_values()
            ratio = max(.15, float(configuration.get("final_test_ratio", .15)))
            development_rows = len(times) - max(1, int(round(len(times) * ratio)))
            if development_rows < 1:
                raise ValueError("No development window.")
            end = pd.Timestamp(times.iloc[development_rows - 1])
            active_end = pd.Timestamp(training.configuration_json["train_end_date"])
            hours = min(168, int((end - active_end) / pd.Timedelta(hours=1)) // 2)
            if hours < settings.retrain_quality_min_hours:
                raise ValueError("Insufficient post-training reference windows.")
            current_start = end - pd.Timedelta(hours=hours - 1)
            baseline_start = current_start - pd.Timedelta(hours=hours)
            reports = [model_quality.get_model_quality(db, region.id, mode="reference", window="replay",
                       start_at=start.isoformat(), hours=hours, expected_model_id=model.id, serving_runtime=serving_runtime)
                       for start in (current_start, baseline_start)]
            decision = quality_decision(*reports, threshold=settings.retrain_quality_mae_increase,
                       min_hours=settings.retrain_quality_min_hours, min_coverage=settings.retrain_quality_min_coverage)
        except (ApplicationError, ValueError, KeyError, TypeError) as error:
            decision = {"status": "unknown", "reason": getattr(error, "code", "quality_window_unavailable")}
    enabled = configuration.get("shap", {}).get("enabled", True)
    return {"enabled": enabled, "mode": "reuse" if decision["status"] == "acceptable" else "refresh",
            "profiles": profiles if decision["status"] == "acceptable" else {},
            "retain_share": .95, "max_mae_increase": .02, "decision": decision}

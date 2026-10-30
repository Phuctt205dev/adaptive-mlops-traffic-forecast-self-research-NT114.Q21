"""Bounded, model-specific SHAP selection on training folds, never Final Test."""
import math
import time

import numpy as np
import pandas as pd

from src.shap_reporting import _values_array, _sample_indices, _take, resolve_shap_config, neural_shap_config

MODEL_KEYS = {"RandomForest": "random_forest_lag", "XGBoost": "xgboost_lag",
              "LightGBM": "lightgbm_lag", "LSTM": "lstm", "GRU": "gru"}


def feature_group(name):
    name = str(name)
    for prefix in ("weather_type_", "weather_description_", "is_holiday_"):
        if name.startswith(prefix):
            return prefix.rstrip("_")
    return {"hour_sin": "hour", "hour_cos": "hour", "day": "day_of_week",
            "dayofweek": "day_of_week", "is_weekend": "day_of_week",
            "day_of_week_sin": "day_of_week", "day_of_week_cos": "day_of_week",
            "month_sin": "month", "month_cos": "month"}.get(name, name)


def validate_selection(config=None):
    config = dict(config or {})
    allowed = {"enabled", "mode", "profiles", "retain_share", "max_mae_increase", "decision"}
    if set(config) - allowed:
        raise ValueError("Unknown feature selection option.")
    if type(config.get("enabled", False)) is not bool or config.get("mode", "refresh") not in {"refresh", "reuse"}:
        raise ValueError("Invalid feature selection mode.")
    for key, default, low, high in (("retain_share", .95, .5, 1.), ("max_mae_increase", .02, 0., .1)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Invalid feature selection threshold.")
    if not isinstance(config.get("profiles", {}), dict) or len(config.get("profiles", {})) > 5:
        raise ValueError("Invalid feature profiles.")
    return config


def reusable_groups(config, model_name, names, train_end):
    profile = config.get("profiles", {}).get(MODEL_KEYS[model_name], {})
    groups = profile.get("selected_groups")
    if (config.get("mode") != "reuse" or profile.get("status") != "validated"
            or profile.get("model_name") != model_name or not isinstance(groups, list)
            or not groups or len(groups) != len(set(groups)) or not all(isinstance(x, str) for x in groups)
            or not set(groups) <= {feature_group(x) for x in names}):
        return None
    if set(profile.get("available_groups", [])) != {feature_group(x) for x in names}:
        return None
    try:
        # A cached selector trained beyond this fold would leak future labels.
        bound, end = pd.Timestamp(profile["selection_train_end"]), pd.Timestamp(train_end)
        if pd.isna(bound) or pd.isna(end) or bound > end:
            return None
    except (KeyError, ValueError, TypeError):
        return None
    return groups


def columns_for_groups(names, groups):
    return [name for name in names if feature_group(name) in groups]


def rank_training_features(model, inputs, names, *, sequence=False, shap_config=None, retain_share=.95):
    """Use bounded training samples/background. Fail closed to full features."""
    import shap
    options = neural_shap_config(shap_config) if sequence else resolve_shap_config(shap_config)
    if not options.enabled:
        raise ValueError("Feature selection requires SHAP enabled.")
    started = time.perf_counter()
    # pandas mixes numeric and one-hot bool columns into object dtype unless
    # explicitly converted. Slice first so conversion stays within the budget.
    dtype = np.float32 if sequence else np.float64
    sample = np.asarray(_take(inputs, _sample_indices(len(inputs), options.max_samples)), dtype=dtype)
    if sample.ndim != (3 if sequence else 2) or sample.shape[-1] != len(names) or not np.isfinite(sample).all():
        raise ValueError("Feature selection input schema differs.")
    if sample.nbytes + options.batch_size * options.nsamples * sample[0].nbytes > 32 * 1024 * 1024:
        raise MemoryError("Feature selection SHAP workload exceeds 32 MiB.")
    if sequence:
        background = np.asarray(_take(inputs, _sample_indices(len(inputs), options.background_samples)), dtype=dtype)
        explainer = shap.GradientExplainer(model, background, batch_size=options.batch_size)
    else:
        explainer = shap.TreeExplainer(model, feature_perturbation="tree_path_dependent", model_output="raw")
    totals = np.zeros(len(names))
    for offset in range(0, len(sample), options.batch_size):
        if time.perf_counter() - started > options.max_seconds:
            raise TimeoutError("Selection SHAP time budget exceeded.")
        batch = sample[offset:offset + options.batch_size]
        state = np.random.get_state()
        try:
            values = (explainer.shap_values(batch, nsamples=options.nsamples, rseed=42 + offset) if sequence
                      else explainer.shap_values(batch, check_additivity=True))
        finally:
            np.random.set_state(state)
        values = _values_array(values, batch.shape)
        totals += np.abs(values).sum(axis=(0, 1) if sequence else 0)
    if time.perf_counter() - started > options.max_seconds or not np.isfinite(totals).all():
        raise ValueError("Selection SHAP exceeded its budget or returned nonfinite values.")
    groups = {}
    for name, score in zip(names, totals):
        key = feature_group(name)
        groups[key] = groups.get(key, 0.) + float(score)
    total = sum(groups.values())
    if total <= 0:
        raise ValueError("No informative SHAP ranking.")
    selected, cumulative = [], 0.
    for key, value in sorted(groups.items(), key=lambda item: (-item[1], item[0])):
        selected.append(key)
        cumulative += value
        if cumulative >= retain_share * total:
            break
    return selected


def selection_summary(model_name, config, folds, names, *, selected_groups=None, selection_train_end=None, reused=False):
    """Choose on CV only. A single failed selection fold preserves full inputs."""
    complete = bool(folds) and all(row.get("reduced_metrics") is not None for row in folds)
    full_mae = float(np.mean([row["full_metrics"][0] for row in folds])) if folds and not reused else None
    reduced_mae = float(np.mean([row["reduced_metrics"][0] for row in folds])) if complete else None
    groups = selected_groups or (folds[-1].get("selected_groups") if folds else None)
    columns = columns_for_groups(names, groups) if groups else list(names)
    reduced = bool(complete and len(columns) < len(names)
                   and (reused or reduced_mae <= full_mae * (1 + config.get("max_mae_increase", .02))))
    selected = groups if reduced else list(dict.fromkeys(feature_group(x) for x in names))
    return {"schema_version": 1, "status": "validated" if reduced else "full",
            "model_name": model_name, "selected_groups": selected,
            "available_groups": sorted({feature_group(x) for x in names}),
            "selected_features": columns if reduced else list(names),
            "selection_train_end": str(selection_train_end), "mode": "reduced" if reduced else "full",
            "selection_metric": "cross_validation_mean_MAE", "final_test_used": False,
            "final_selection_source": "last_training_fold",
            "full_cv_mae": full_mae, "reduced_cv_mae": reduced_mae,
            "profile_reused": reused, "retain_share": config.get("retain_share", .95),
            "max_mae_increase": config.get("max_mae_increase", .02), "folds": folds}


def guard_quality_holdout(config, final_test_start):
    config = validate_selection(config)
    end = config.get("decision", {}).get("quality_window_end")
    if end:
        try:
            bound, holdout = pd.Timestamp(end), pd.Timestamp(final_test_start)
            if bound.tzinfo is not None:
                bound = bound.tz_localize(None)  # Dataset uses region wall time.
            if holdout.tzinfo is not None:
                holdout = holdout.tz_localize(None)
            valid = bound < holdout
        except (TypeError, ValueError):
            valid = False
        if not valid:
            return {**config, "mode": "refresh", "profiles": {},
                    "decision": {"status": "unknown", "reason": "quality_overlaps_holdout"}}
    return config


def wrap_tree_model(model, all_names, selected_names):
    """Use only sklearn builtins so old serving workers need no custom class."""
    from sklearn.compose import ColumnTransformer
    from sklearn.pipeline import Pipeline
    selector = ColumnTransformer([("selected", "passthrough", selected_names)],
                                 remainder="drop", verbose_feature_names_out=False)
    selector.fit(pd.DataFrame(np.zeros((1, len(all_names))), columns=all_names))
    selector.set_output(transform="pandas")
    return Pipeline([("preprocessor", selector), ("model", model)])

from __future__ import annotations

import math

import numpy as np
import pandas as pd


DEFAULT_EXCLUDED_COLUMNS = {"date_time", "traffic_volume"}


def _safe_float(value):
    if value is None or pd.isna(value):
        return None
    return float(value)


def _numeric_psi(reference: pd.Series, current: pd.Series, bins: int = 10) -> float:
    reference_values = pd.to_numeric(reference, errors="coerce").dropna().to_numpy(dtype=float)
    current_values = pd.to_numeric(current, errors="coerce").dropna().to_numpy(dtype=float)
    if len(reference_values) == 0 or len(current_values) == 0:
        return 0.0

    quantiles = np.linspace(0, 1, bins + 1)
    edges = np.unique(np.quantile(reference_values, quantiles))
    if len(edges) < 2:
        min_value = min(reference_values.min(), current_values.min())
        max_value = max(reference_values.max(), current_values.max())
        if math.isclose(min_value, max_value):
            return 0.0
        edges = np.linspace(min_value, max_value, bins + 1)

    edges[0] = min(edges[0], current_values.min()) - 1e-9
    edges[-1] = max(edges[-1], current_values.max()) + 1e-9
    reference_counts, _ = np.histogram(reference_values, bins=edges)
    current_counts, _ = np.histogram(current_values, bins=edges)

    epsilon = 1e-6
    reference_ratio = np.maximum(reference_counts / max(reference_counts.sum(), 1), epsilon)
    current_ratio = np.maximum(current_counts / max(current_counts.sum(), 1), epsilon)
    return float(np.sum((current_ratio - reference_ratio) * np.log(current_ratio / reference_ratio)))


def _categorical_js_divergence(reference: pd.Series, current: pd.Series) -> float:
    reference_counts = reference.fillna("__missing__").astype(str).value_counts(normalize=True)
    current_counts = current.fillna("__missing__").astype(str).value_counts(normalize=True)
    categories = reference_counts.index.union(current_counts.index)
    p = reference_counts.reindex(categories, fill_value=0.0).to_numpy(dtype=float)
    q = current_counts.reindex(categories, fill_value=0.0).to_numpy(dtype=float)
    m = 0.5 * (p + q)

    def kl_divergence(left, right):
        mask = left > 0
        return float(np.sum(left[mask] * np.log(left[mask] / right[mask])))

    return 0.5 * kl_divergence(p, m) + 0.5 * kl_divergence(q, m)


def calculate_feature_drift(
    reference_df: pd.DataFrame,
    current_df: pd.DataFrame,
    numeric_threshold: float = 0.2,
    categorical_threshold: float = 0.15,
    min_drifted_features: int = 2,
    excluded_columns: set[str] | None = None,
) -> dict:
    excluded = DEFAULT_EXCLUDED_COLUMNS if excluded_columns is None else excluded_columns
    common_columns = [
        column
        for column in reference_df.columns
        if column in current_df.columns and column not in excluded
    ]

    features = {}
    drifted_count = 0
    max_numeric_psi = 0.0
    max_categorical_js = 0.0

    for column in common_columns:
        reference_column = reference_df[column]
        current_column = current_df[column]
        is_numeric = (
            pd.api.types.is_numeric_dtype(reference_column)
            and pd.api.types.is_numeric_dtype(current_column)
        )
        if is_numeric:
            value = _numeric_psi(reference_column, current_column)
            threshold = float(numeric_threshold)
            metric = "psi"
            feature_type = "numeric"
            max_numeric_psi = max(max_numeric_psi, value)
        else:
            value = _categorical_js_divergence(reference_column, current_column)
            threshold = float(categorical_threshold)
            metric = "js_divergence"
            feature_type = "categorical"
            max_categorical_js = max(max_categorical_js, value)

        drift = bool(value > threshold)
        if drift:
            drifted_count += 1
        features[column] = {
            "type": feature_type,
            "metric": metric,
            "value": round(float(value), 6),
            "threshold": threshold,
            "drift": drift,
        }

    drift_detected = drifted_count >= int(min_drifted_features)
    return {
        "summary": {
            "feature_count": len(common_columns),
            "drifted_feature_count": drifted_count,
            "drift_detected": drift_detected,
            "min_drifted_features": int(min_drifted_features),
            "max_numeric_psi": round(max_numeric_psi, 6),
            "max_categorical_js_divergence": round(max_categorical_js, 6),
        },
        "features": features,
    }

"""Shared model selection names; no fitting, framework or storage imports."""

TREE_LAG_VARIANTS = {
    "random_forest_lag": "RandomForest",
    "xgboost_lag": "XGBoost",
    "lightgbm_lag": "LightGBM",
}
RECURRENT_VARIANTS = {"lstm": "LSTM", "gru": "GRU"}
PRODUCTION_MODEL_VARIANTS = (*TREE_LAG_VARIANTS, *RECURRENT_VARIANTS)
DEFAULT_PRODUCTION_MODEL_VARIANTS = tuple(PRODUCTION_MODEL_VARIANTS)


def normalize_selected_models(selected_models):
    if not selected_models:
        return list(DEFAULT_PRODUCTION_MODEL_VARIANTS)
    normalized = []
    for model in selected_models:
        value = str(model).strip().lower()
        if value not in PRODUCTION_MODEL_VARIANTS:
            raise ValueError(f"Unsupported training model variant: {model}.")
        if value not in normalized:
            normalized.append(value)
    return normalized

from src.models.lightgbm import (
    build_autoregressive_lightgbm,
    build_original_lightgbm,
)
from src.models.random_forest import (
    build_autoregressive_random_forest,
    build_original_random_forest,
)
from src.models.xgboost import (
    build_autoregressive_xgboost,
    build_original_xgboost,
)


ORIGINAL_MODEL_BUILDERS = {
    "RandomForest": build_original_random_forest,
    "XGBoost": build_original_xgboost,
    "LightGBM": build_original_lightgbm,
}

AUTOREGRESSIVE_MODEL_BUILDERS = {
    "RandomForest": build_autoregressive_random_forest,
    "XGBoost": build_autoregressive_xgboost,
    "LightGBM": build_autoregressive_lightgbm,
}

ORIGINAL_MODEL_NAMES = tuple(ORIGINAL_MODEL_BUILDERS)
AUTOREGRESSIVE_MODEL_NAMES = tuple(AUTOREGRESSIVE_MODEL_BUILDERS)


def _build_model(model_name, builders, profile_name, random_state):
    try:
        builder = builders[model_name]
    except KeyError as error:
        supported = ", ".join(builders)
        raise ValueError(
            f"Model không được hỗ trợ cho {profile_name}: "
            f"{model_name}. Chỉ dùng: {supported}."
        ) from error
    return builder(random_state)


def build_original_model(model_name, random_state):
    """Tạo model cho pipeline gốc, không có feature lag/rolling."""
    return _build_model(
        model_name,
        ORIGINAL_MODEL_BUILDERS,
        "original",
        random_state,
    )


def build_autoregressive_model(model_name, random_state):
    """Tạo model cho pipeline time-series có feature lag/rolling."""
    return _build_model(
        model_name,
        AUTOREGRESSIVE_MODEL_BUILDERS,
        "autoregressive",
        random_state,
    )

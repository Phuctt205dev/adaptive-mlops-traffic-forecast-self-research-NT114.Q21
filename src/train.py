"""API tương thích cho code cũ; định nghĩa model nằm trong src/models/."""

from src.models import build_original_model


def _train_original_model(
    model_name,
    X_train,
    y_train,
    random_state,
):
    model = build_original_model(model_name, random_state)
    model.fit(X_train, y_train)
    return model


def train_random_forest(X_train, y_train, random_state):
    return _train_original_model(
        "RandomForest",
        X_train,
        y_train,
        random_state,
    )


def train_xgboost(X_train, y_train, random_state):
    return _train_original_model(
        "XGBoost",
        X_train,
        y_train,
        random_state,
    )


def train_lightgbm(X_train, y_train, random_state):
    return _train_original_model(
        "LightGBM",
        X_train,
        y_train,
        random_state,
    )

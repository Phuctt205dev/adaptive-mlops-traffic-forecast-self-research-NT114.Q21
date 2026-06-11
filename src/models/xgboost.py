from xgboost import XGBRegressor


def build_original_xgboost(random_state):
    """XGBoost của pipeline production ban đầu."""
    return XGBRegressor(
        n_estimators=300,
        max_depth=6,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=random_state,
    )


def build_autoregressive_xgboost(random_state):
    """XGBoost thử nghiệm với feature lag/rolling."""
    return XGBRegressor(
        n_estimators=500,
        learning_rate=0.04,
        max_depth=7,
        min_child_weight=2,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_lambda=1.0,
        objective="reg:squarederror",
        random_state=random_state,
        n_jobs=-1,
        tree_method="hist",
    )

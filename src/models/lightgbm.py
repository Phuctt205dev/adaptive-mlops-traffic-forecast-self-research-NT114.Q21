from lightgbm import LGBMRegressor


def build_original_lightgbm(random_state):
    """LightGBM của pipeline production ban đầu."""
    return LGBMRegressor(
        n_estimators=300,
        max_depth=-1,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=random_state,
    )


def build_autoregressive_lightgbm(random_state):
    """LightGBM thử nghiệm với feature lag/rolling."""
    return LGBMRegressor(
        n_estimators=500,
        learning_rate=0.04,
        num_leaves=31,
        max_depth=-1,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_lambda=0.1,
        random_state=random_state,
        n_jobs=-1,
        verbosity=-1,
    )

from sklearn.ensemble import RandomForestRegressor
from src.models.cpu_budget import training_threads


def build_original_random_forest(random_state):
    """Random Forest của pipeline ban đầu, không yêu cầu lag/rolling."""
    return RandomForestRegressor(
        n_estimators=100,
        random_state=random_state,
    )


def build_autoregressive_random_forest(random_state):
    """Random Forest dùng bảng feature time-series có lag/rolling."""
    return RandomForestRegressor(
        n_estimators=100,
        random_state=random_state,
        n_jobs=training_threads(),
    )

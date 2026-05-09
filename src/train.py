# src/train.py

from sklearn.ensemble import RandomForestRegressor
from xgboost import XGBRegressor
from lightgbm import LGBMRegressor


# =========================
# RANDOM FOREST
# =========================
def train_random_forest(X_train, y_train, random_state):
    model = RandomForestRegressor(
        n_estimators=100,
        random_state=random_state
    )
    model.fit(X_train, y_train)
    return model


# =========================
# XGBOOST
# =========================
def train_xgboost(X_train, y_train, random_state):
    model = XGBRegressor(
        n_estimators=300,
        max_depth=6,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=random_state
    )
    model.fit(X_train, y_train)
    return model


# =========================
# LIGHTGBM
# =========================
def train_lightgbm(X_train, y_train, random_state):
    model = LGBMRegressor(
        n_estimators=300,
        max_depth=-1,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=random_state
    )
    model.fit(X_train, y_train)
    return model
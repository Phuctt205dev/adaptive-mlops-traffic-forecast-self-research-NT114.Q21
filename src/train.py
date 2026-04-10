from sklearn.ensemble import RandomForestRegressor
from xgboost import XGBRegressor

# =========================
# RANDOM FOREST
# =========================
def train_random_forest(X_train, y_train):
    model = RandomForestRegressor(
        n_estimators=100
    )
    model.fit(X_train, y_train)
    return model

# =========================
# XGBOOST
# =========================
def train_xgboost(X_train, y_train):
    model = XGBRegressor(
        n_estimators=300,
        max_depth=6,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8
    )
    model.fit(X_train, y_train)
    return model
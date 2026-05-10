# predict.py
import joblib
import matplotlib.pyplot as plt
import os

from sklearn.metrics import mean_absolute_error

from src.preprocess import (
    load_data,
    preprocess
)

print("🚀 Predicting...")

# =========================
# CHỈNH TAY
# =========================
PREDICT_START = "2013-12-01"
PREDICT_END   = "2014-01-01"

# =========================
# LOAD MODEL
# =========================
model = joblib.load(
    "models/best_model.pkl"
)

# =========================
# LOAD DATA
# =========================
df = load_data(
    "data/TrafficVolumeData.csv"
)

df = preprocess(df)

# =========================
# FILTER
# =========================
df_pred = df[
    (df["date_time"] >= PREDICT_START)
    &
    (df["date_time"] < PREDICT_END)
].copy()

# =========================
# FEATURES
# =========================
X = df_pred.drop(
    ["traffic_volume", "date_time"],
    axis=1
)

X = X.reindex(
    columns=model.feature_names_in_,
    fill_value=0
)

# =========================
# PREDICT
# =========================
preds = model.predict(X)

df_pred["prediction"] = preds

# =========================
# MAE
# =========================
mae = mean_absolute_error(
    df_pred["traffic_volume"],
    preds
)

print(
    f"MAE: {mae:.2f}"
)

# =========================
# SAVE
# =========================
os.makedirs(
    "results",
    exist_ok=True
)

df_pred.to_csv(
    "results/predict.csv",
    index=False
)

print(
    "✅ Saved: results/predict.csv"
)

# =========================
# PLOT
# =========================
plt.figure(
    figsize=(14, 6)
)

plt.plot(
    df_pred["date_time"],
    df_pred["traffic_volume"],
    label="Actual"
)

plt.plot(
    df_pred["date_time"],
    df_pred["prediction"],
    label="Prediction"
)

plt.xticks(
    rotation=45
)

plt.legend()

plt.title(
    "Traffic Prediction"
)

plt.tight_layout()
plt.show()
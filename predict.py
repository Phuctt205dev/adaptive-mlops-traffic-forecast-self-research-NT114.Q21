import pandas as pd
import joblib
import matplotlib.pyplot as plt
import os

from src.preprocess import load_data, preprocess

print("🚀 Predicting REAL 2014 data...")

# =========================
# LOAD MODEL
# =========================
model = joblib.load("models/best_model.pkl")

# =========================
# LOAD + PREPROCESS DATA
# =========================
df = load_data("data/TrafficVolumeData.csv")
df = preprocess(df)

# =========================
# FILTER 2014
# =========================
df_2014 = df[df["date_time"] >= "2014-01-02"].copy()

# =========================
# PREPARE FEATURES
# =========================
X = df_2014.drop(["traffic_volume", "date_time"], axis=1)

# ⚠️ đảm bảo đúng thứ tự cột
model_features = model.feature_names_in_
X = X.reindex(columns=model_features, fill_value=0)

# =========================
# PREDICT
# =========================
preds = model.predict(X)

df_2014["prediction"] = preds

# =========================
# ERROR
# =========================
df_2014["error"] = df_2014["traffic_volume"] - df_2014["prediction"]

# =========================
# SAVE CSV
# =========================
os.makedirs("results", exist_ok=True)
df_2014.to_csv("results/predict_2014.csv", index=False)

print("✅ Saved: results/predict_2014.csv")

# =========================
# 📊 PLOT (1 THÁNG)
# =========================
plt.figure(figsize=(14, 6))

# lấy đúng 1 tháng đầu tiên
sample = df_2014[
    (df_2014["date_time"] >= "2014-01-02") &
    (df_2014["date_time"] < "2014-02-02")
]

plt.plot(
    sample["date_time"],
    sample["traffic_volume"],
    label="Actual"
)

plt.plot(
    sample["date_time"],
    sample["prediction"],
    label="Prediction"
)

plt.xticks(rotation=45)
plt.legend()
plt.title("Traffic Prediction vs Actual (January 2014)")
plt.tight_layout()

plt.show()
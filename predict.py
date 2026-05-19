# predict.py
import joblib
import matplotlib.pyplot as plt
import os
import pandas as pd

from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error,
    mean_absolute_percentage_error
)

from datetime import datetime

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
# GET CURRENT MODEL VERSION
# =========================
model_version = "unknown"

version_file = (
    "models/model_versions.csv"
)

if os.path.exists(
    version_file
):
    version_df = pd.read_csv(
        version_file
    )

    if len(version_df) > 0:
        model_version = (
            version_df.iloc[-1]["version"]
        )

print(
    "🧠 Current model:",
    model_version
)


# =========================
# LOAD DATA
# =========================
df = load_data(
    "data/TrafficVolumeData.csv"
)

df = preprocess(
    df
)


# =========================
# FILTER
# =========================
df_pred = df[
    (df["date_time"] >= PREDICT_START)
    &
    (df["date_time"] < PREDICT_END)
].copy()

if len(df_pred) == 0:
    print(
        "❌ No data found for prediction."
    )
    exit()


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
preds = model.predict(
    X
)

df_pred["prediction"] = preds


# =========================
# METRICS
# =========================
mae = mean_absolute_error(
    df_pred["traffic_volume"],
    preds
)

rmse = mean_squared_error(
    df_pred["traffic_volume"],
    preds
) ** 0.5

mape = (
    mean_absolute_percentage_error(
        df_pred["traffic_volume"],
        preds
    ) * 100
)

print(
    f"MAE  : {mae:.2f}"
)

print(
    f"RMSE : {rmse:.2f}"
)

print(
    f"MAPE : {mape:.2f}%"
)


# =========================
# SAVE RESULT
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
# PREDICT LOG
# =========================
log_file = (
    "results/predict_log.csv"
)

new_row = pd.DataFrame(
    [{
        "timestamp":
        datetime.now(),

        "model_version":
        model_version,

        "mae":
        mae,

        "rmse":
        rmse,

        "mape":
        mape
    }]
)

if os.path.exists(
    log_file
):
    old = pd.read_csv(
        log_file
    )

    all_logs = pd.concat(
        [old, new_row],
        ignore_index=True
    )

else:
    all_logs = new_row

all_logs.to_csv(
    log_file,
    index=False
)

print(
    "📝 Predict log saved"
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
    f"Traffic Prediction ({model_version})"
)

plt.tight_layout()
plt.show()
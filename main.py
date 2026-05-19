# main.py
import os
import joblib
import subprocess

from src.preprocess import (
    load_data,
    preprocess
)

from src.pipeline import (
    run_pipeline
)

from src.drift import (
    detect_drift_by_mae
)

# ====================================
# CHỈNH TAY
# ====================================

# train initial
TRAIN_START_DATE = "2013-01-01"
TRAIN_END_DATE   = "2013-06-01"

# tháng mới cần predict
PREDICT_START = "2013-12-01"
PREDICT_END   = "2014-01-01"

# drift threshold
MAE_THRESHOLD = 100

MODEL_PATH = "models/best_model.pkl"


# ====================================
# FIRST RUN?
# ====================================

if not os.path.exists(MODEL_PATH):

    print(
        "⚠️ No model found."
    )

    print(
        "🚀 Training initial model..."
    )

    run_pipeline(
        train_start_date=
        TRAIN_START_DATE,

        train_end_date=
        TRAIN_END_DATE
    )

    print(
        "\n✅ Initial model created."
    )

    print(
        "Run main.py again."
    )

    exit()


# ====================================
# LOAD DATA
# ====================================

df = load_data(
    "data/TrafficVolumeData.csv"
)

df = preprocess(
    df
)


# ====================================
# LOAD MODEL
# ====================================

print(
    "\n📦 Loading current model..."
)

model = joblib.load(
    MODEL_PATH
)

print(
    "✅ Using:",
    MODEL_PATH
)


# ====================================
# FILTER NEW MONTH
# ====================================

new_month = df[
    (df["date_time"] >= PREDICT_START)
    &
    (df["date_time"] < PREDICT_END)
].copy()

if len(new_month) == 0:
    print(
        "❌ No data found for prediction window."
    )
    exit()


# ====================================
# FEATURES
# ====================================

X = new_month.drop(
    ["traffic_volume", "date_time"],
    axis=1
)

X = X.reindex(
    columns=model.feature_names_in_,
    fill_value=0
)

y_true = new_month[
    "traffic_volume"
]


# ====================================
# PREDICT
# ====================================

print(
    "\n🔮 Predicting..."
)

y_pred = model.predict(
    X
)


# ====================================
# DRIFT CHECK
# ====================================

drift, mae = detect_drift_by_mae(
    y_true,
    y_pred,
    mae_threshold=
    MAE_THRESHOLD
)


# ====================================
# NEW TRAIN WINDOW
# ====================================

new_train_start = "2013-08-01"
new_train_end   = "2014-01-01"

print(
    "\n📦 New train window:"
)

print(
    f"{new_train_start}"
    f" → "
    f"{new_train_end}"
)


# ====================================
# RETRAIN
# ====================================

if drift:

    print(
        "\n🔄 Drift found → retrain"
    )

    run_pipeline(
        train_start_date=
        new_train_start,

        train_end_date=
        new_train_end
    )

    print(
        "\n✅ Retrain complete."
    )

    print(
        "🚀 Running predict.py on new model..."
    )

    subprocess.run(
        ["python", "predict.py"]
    )

else:

    print(
        "\n✅ No drift"
    )

    print(
        "Keep current model."
    )
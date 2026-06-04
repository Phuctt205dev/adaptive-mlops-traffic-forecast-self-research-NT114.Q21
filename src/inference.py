# src/inference.py
import joblib
import pandas as pd

from src.preprocess import (
    preprocess
)


# =========================
# LOAD MODEL
# =========================
def load_model():
    model = joblib.load(
        "models/best_model.pkl"
    )

    return model


# =========================
# PREPARE INPUT
# =========================
def prepare_input(
    raw_input,
    model
):
    # =====================
    # DICT -> DATAFRAME
    # =====================
    df = pd.DataFrame(
        [raw_input]
    )

    # =====================
    # ADD DUMMY TARGET
    # preprocess() hiện tại
    # cần traffic_volume
    # =====================
    df["traffic_volume"] = 0

    # =====================
    # PREPROCESS
    # =====================
    df = preprocess(
        df
    )

    # =====================
    # DROP UNUSED COLUMNS
    # =====================
    X = df.drop(
        [
            "traffic_volume",
            "date_time"
        ],
        axis=1
    )

    # =====================
    # ALIGN FEATURES
    # IMPORTANT:
    # match training columns
    # =====================
    X = X.reindex(
        columns=model.feature_names_in_,
        fill_value=0
    )

    return X


# =========================
# PREDICT SINGLE
# =========================
def predict_single(
    raw_input
):
    print(
        "\n📦 Loading model..."
    )

    model = load_model()

    print(
        "✅ Model loaded"
    )

    print(
        "🛠 Preparing input..."
    )

    X = prepare_input(
        raw_input,
        model
    )

    print(
        "🔮 Predicting..."
    )

    pred = model.predict(
        X
    )[0]

    return float(
        pred
    )
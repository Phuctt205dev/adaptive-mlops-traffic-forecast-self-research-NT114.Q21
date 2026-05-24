# app.py
from fastapi import FastAPI
from src.inference import (
    predict_single
)

app = FastAPI(
    title="Traffic Volume Prediction API"
)


# =========================
# HEALTH CHECK
# =========================
@app.get(
    "/health"
)
def health():
    return {
        "status": "ok"
    }


# =========================
# MODEL INFO
# =========================
@app.get(
    "/model-info"
)
def model_info():
    return {
        "model_file":
        "models/best_model.pkl"
    }


# =========================
# PREDICT
# =========================
@app.post(
    "/predict"
)
def predict(
    data: dict
):
    prediction = predict_single(
        data
    )

    return {
        "prediction":
        prediction
    }
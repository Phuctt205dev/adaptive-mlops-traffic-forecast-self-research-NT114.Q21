# app.py
from fastapi import FastAPI
from fastapi.middleware.cors import (
    CORSMiddleware
)

from src.inference import (
    predict_single
)

app = FastAPI(
    title="Traffic Volume Prediction API"
)

# =========================
# CORS
# Cho phép GitHub Pages
# gọi API từ domain khác
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
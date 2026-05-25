# app.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.inference import predict_single

app = FastAPI(title="Traffic Volume Prediction API")

# =========================
# CORS (GIỮ - nhưng chỉnh an toàn hơn)
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://phuctt205dev.github.io",
        "https://traffic-son.duckdns.org"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# REQUEST MODEL (QUAN TRỌNG)
# =========================
class PredictRequest(BaseModel):
    date_time: str
    is_holiday: int | None = None
    air_pollution_index: int
    humidity: int
    wind_speed: int
    wind_direction: int
    visibility_in_miles: int
    dew_point: int
    temperature: int
    rain_p_h: float
    snow_p_h: float
    clouds_all: int
    weather_type: str
    weather_description: str


# =========================
# HEALTH CHECK
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}


# =========================
# MODEL INFO
# =========================
@app.get("/model-info")
def model_info():
    return {
        "model_file": "models/best_model.pkl"
    }


# =========================
# PREDICT
# =========================
@app.post("/predict")
def predict(data: PredictRequest):
    prediction = predict_single(data.dict())

    return {
        "prediction": prediction
    }
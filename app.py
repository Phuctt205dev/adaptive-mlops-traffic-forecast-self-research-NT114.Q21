# app.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests

from src.inference import predict_single

app = FastAPI(title="Traffic Volume Prediction API")

# =========================
# CORS (GIỮ NGUYÊN)
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
# REQUEST MODEL (MỚI - đơn giản hơn)
# Chỉ cần date_time
# =========================
class PredictRequest(BaseModel):
    date_time: str


# =========================
# WEATHER API
# Minneapolis coordinates
# =========================
def get_weather_features():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=44.98"
        "&longitude=-93.26"
        "&hourly="
        "temperature_2m,"
        "relative_humidity_2m,"
        "cloud_cover,"
        "precipitation"
    )

    response = requests.get(url)
    data = response.json()

    return {
        "temperature": data["hourly"]["temperature_2m"][0],
        "humidity": data["hourly"]["relative_humidity_2m"][0],
        "clouds_all": data["hourly"]["cloud_cover"][0],
        "rain_p_h": data["hourly"]["precipitation"][0],
    }


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
    weather = get_weather_features()

    payload = {
        "date_time": data.date_time,

        # giữ default cho các feature còn lại
        "is_holiday": None,
        "air_pollution_index": 121,
        "humidity": weather["humidity"],
        "wind_speed": 2,
        "wind_direction": 329,
        "visibility_in_miles": 1,
        "dew_point": 1,
        "temperature": weather["temperature"],
        "rain_p_h": weather["rain_p_h"],
        "snow_p_h": 0,
        "clouds_all": weather["clouds_all"],
        "weather_type": "Clouds",
        "weather_description": "scattered clouds"
    }

    prediction = predict_single(payload)

    return {
        "prediction": prediction
    }
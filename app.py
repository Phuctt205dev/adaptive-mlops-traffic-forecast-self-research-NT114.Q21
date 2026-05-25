# app.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import pandas as pd

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
# REQUEST MODEL (GIỮ)
# Chỉ cần date_time
# =========================
class PredictRequest(BaseModel):
    date_time: str


# =========================
# WEATHER TYPE MAPPING
# Open-Meteo weather_code -> model label
# =========================
def map_weather_type(code):
    if code == 0:
        return "Clear"

    elif code in [1, 2, 3, 45, 48]:
        return "Clouds"

    elif code in [
        51, 53, 55,
        56, 57,
        61, 63, 65,
        66, 67,
        80, 81, 82
    ]:
        return "Rain"

    elif code in [
        71, 73, 75,
        77,
        85, 86
    ]:
        return "Snow"

    return "Clouds"


# =========================
# HOLIDAY CHECK
# Tạm dùng weekend
# =========================
def get_is_holiday(date_str):
    dt = pd.to_datetime(date_str)

    # Saturday=5, Sunday=6
    if dt.weekday() >= 5:
        return 1

    return 0


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
        "dew_point_2m,"
        "precipitation,"
        "snowfall,"
        "cloud_cover,"
        "visibility,"
        "wind_speed_10m,"
        "wind_direction_10m,"
        "weather_code"
    )

    response = requests.get(url)
    data = response.json()

    return {
        "temperature":
            data["hourly"]["temperature_2m"][0],

        "humidity":
            data["hourly"]["relative_humidity_2m"][0],

        "dew_point":
            data["hourly"]["dew_point_2m"][0],

        "rain_p_h":
            data["hourly"]["precipitation"][0],

        "snow_p_h":
            data["hourly"]["snowfall"][0],

        "clouds_all":
            data["hourly"]["cloud_cover"][0],

        # convert meter -> miles
        "visibility_in_miles":
            data["hourly"]["visibility"][0] / 1609.34,

        "wind_speed":
            data["hourly"]["wind_speed_10m"][0],

        "wind_direction":
            data["hourly"]["wind_direction_10m"][0],

        "weather_code":
            data["hourly"]["weather_code"][0],
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
        "date_time":
            data.date_time,

        "is_holiday":
            get_is_holiday(data.date_time),

        # chưa có air pollution API
        "air_pollution_index":
            121,

        "humidity":
            weather["humidity"],

        "wind_speed":
            weather["wind_speed"],

        "wind_direction":
            weather["wind_direction"],

        "visibility_in_miles":
            weather["visibility_in_miles"],

        "dew_point":
            weather["dew_point"],

        "temperature":
            weather["temperature"],

        "rain_p_h":
            weather["rain_p_h"],

        "snow_p_h":
            weather["snow_p_h"],

        "clouds_all":
            weather["clouds_all"],

        "weather_type":
            map_weather_type(
                weather["weather_code"]
            ),

        "weather_description":
            "auto_generated"
    }

    prediction = predict_single(payload)

    return {
        "prediction": prediction
    }
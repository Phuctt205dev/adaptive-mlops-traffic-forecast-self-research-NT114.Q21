# app.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import requests
import pandas as pd

from src.inference import predict_single

app = FastAPI(title="Traffic Volume Prediction API")


# =========================
# FRONTEND STATIC FILES
# =========================
app.mount(
    "/web",
    StaticFiles(directory="docs"),
    name="web"
)


@app.get("/")
def root():
    return FileResponse(
        "docs/index.html"
    )


# =========================
# CORS (GIỮ NGUYÊN)
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://phuctt205dev.github.io",
        "https://traffic-son.duckdns.org",
        "http://traffic-son.duckdns.org",
        "http://traffic-son.duckdns.org:8000",
        "http://localhost:5173",
        "http://127.0.0.1:5173"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# REQUEST MODEL (GIỮ)
# =========================
class PredictRequest(BaseModel):
    date_time: str


# =========================
# WEATHER TYPE MAPPING
# =========================
def map_weather_type(code):
    if code == 0:
        return "Clear"
    elif code in [1, 2, 3, 45, 48]:
        return "Clouds"
    elif code in [51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82]:
        return "Rain"
    elif code in [71, 73, 75, 77, 85, 86]:
        return "Snow"
    return "Clouds"


# =========================
# HOLIDAY CHECK
# =========================
def get_is_holiday(date_str):
    dt = pd.to_datetime(date_str)
    return 1 if dt.weekday() >= 5 else 0


# =========================
# WEATHER API (FIXED - AN TOÀN)
# =========================
def get_weather_features(target_datetime):

    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=44.98"
        "&longitude=-93.26"
        "&timezone=auto"
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

    try:

        response = requests.get(
            url,
            timeout=8
        )

        response.raise_for_status()

        data = response.json()

        hourly = data["hourly"]

        # convert user datetime
        target_dt = pd.to_datetime(
            target_datetime
        )

        api_times = pd.to_datetime(
            hourly["time"]
        )

        # DEBUG
        print("USER TIME:", target_dt)
        print("FIRST API TIME:", api_times[0])

        # tìm nearest hour
        idx = min(
            range(len(api_times)),
            key=lambda i:
            abs(
                (
                    api_times[i] - target_dt
                ).total_seconds()
            )
        )

        print("MATCHED INDEX:", idx)
        print("MATCHED TIME:", api_times[idx])

        return {

            "temperature":
                hourly["temperature_2m"][idx],

            "humidity":
                hourly["relative_humidity_2m"][idx],

            "dew_point":
                hourly["dew_point_2m"][idx],

            "rain_p_h":
                hourly["precipitation"][idx],

            "snow_p_h":
                hourly["snowfall"][idx],

            "clouds_all":
                hourly["cloud_cover"][idx],

            "visibility_in_miles":
                hourly["visibility"][idx] / 1609.34,

            "wind_speed":
                hourly["wind_speed_10m"][idx],

            "wind_direction":
                hourly["wind_direction_10m"][idx],

            "weather_code":
                hourly["weather_code"][idx],
        }

    except Exception as e:

        print("Weather API FAILED:", e)

        return {

            "temperature": 20,
            "humidity": 60,
            "dew_point": 10,
            "rain_p_h": 0,
            "snow_p_h": 0,
            "clouds_all": 30,
            "visibility_in_miles": 10,
            "wind_speed": 5,
            "wind_direction": 180,
            "weather_code": 1,
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

    weather = get_weather_features(data.date_time)

    payload = {
        "date_time": data.date_time,
        "is_holiday": get_is_holiday(data.date_time),
        "air_pollution_index": 121,
        "humidity": weather["humidity"],
        "wind_speed": weather["wind_speed"],
        "wind_direction": weather["wind_direction"],
        "visibility_in_miles": weather["visibility_in_miles"],
        "dew_point": weather["dew_point"],
        "temperature": weather["temperature"],
        "rain_p_h": weather["rain_p_h"],
        "snow_p_h": weather["snow_p_h"],
        "clouds_all": weather["clouds_all"],
        "weather_type": map_weather_type(weather["weather_code"]),
        "weather_description": "auto_generated"
    }

    prediction = predict_single(payload)

    return {
        "prediction": int(round(prediction)),
        "features_used": payload
    }
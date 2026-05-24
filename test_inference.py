# test_inference.py
from src.inference import (
    predict_single
)

sample = {
    "date_time":
    "2013-12-01 08:00:00",

    "is_holiday":
    None,

    "air_pollution_index":
    121,

    "humidity":
    89,

    "wind_speed":
    2,

    "wind_direction":
    329,

    "visibility_in_miles":
    1,

    "dew_point":
    1,

    "temperature":
    40,

    "rain_p_h":
    0,

    "snow_p_h":
    0,

    "clouds_all":
    40,

    "weather_type":
    "Clouds",

    "weather_description":
    "scattered clouds"
}

result = predict_single(
    sample
)

print(
    "\nPrediction:",
    result
)
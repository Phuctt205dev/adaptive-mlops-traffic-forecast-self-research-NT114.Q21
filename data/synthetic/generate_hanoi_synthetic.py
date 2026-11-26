"""Generate synthetic Hanoi hourly traffic data for 2026-2030.

The HCMC synthetic file supplies source-derived traffic patterns. All rows in
this output are simulated; neither city's file contains measured local traffic.
"""

from pathlib import Path

import numpy as np
import pandas as pd


DATA_DIR = Path(__file__).resolve().parent
SOURCE = DATA_DIR / "TrafficVolumeData_HCM_2015_2030_synthetic.csv"
OUTPUT = DATA_DIR / "TrafficVolumeData_Hanoi_2026_2030_synthetic.csv"
SEED = 20261003

# Assumed monthly means in Celsius for a four-season simulation, not observations.
MONTHLY_MEAN_C = np.array([17, 18, 21, 24, 28, 30, 30, 29, 28, 25, 22, 19])


def main() -> None:
    rng = np.random.default_rng(SEED)
    frame = pd.read_csv(SOURCE, keep_default_na=False)
    timestamps = pd.to_datetime(frame["date_time"], errors="raise")
    frame = frame.loc[
        (timestamps >= pd.Timestamp("2026-01-01"))
        & (timestamps <= pd.Timestamp("2030-12-31 23:00:00"))
    ].copy().reset_index(drop=True)
    times = pd.to_datetime(frame["date_time"])
    expected = pd.date_range("2026-01-01", "2030-12-31 23:00:00", freq="h")
    if len(frame) != len(expected) or not np.array_equal(times.to_numpy(), expected.to_numpy()):
        raise ValueError("The source does not cover every hour in 2026-2030.")

    count = len(frame)
    days = pd.date_range("2026-01-01", "2030-12-31", freq="D")
    month = times.dt.month.to_numpy()
    hour = times.dt.hour.to_numpy()
    weekday = times.dt.dayofweek.to_numpy()
    source_rain = pd.to_numeric(frame["rain_p_h"]).to_numpy() > 0

    # Independent wet-season showers and lighter cool-season drizzle.
    wet_day = np.isin(days.month.to_numpy(), [5, 6, 7, 8, 9, 10])
    rain_day = rng.random(len(days)) < np.where(wet_day, 0.47, 0.19)
    rain_start = np.where(
        wet_day, rng.integers(13, 21, len(days)), rng.integers(5, 12, len(days))
    )
    rain_end = rain_start + rng.integers(1, 5, len(days))
    hours_of_day = np.arange(24)[None, :]
    raining = (
        rain_day[:, None]
        & (hours_of_day >= rain_start[:, None])
        & (hours_of_day < rain_end[:, None])
    ).ravel()
    rain_intensity = np.where(
        np.repeat(wet_day, 24), rng.gamma(1.7, 1.6, count), rng.gamma(1.1, 0.5, count)
    )
    rain_amount = np.where(raining, np.maximum(rain_intensity, 0.1), 0.0)
    frame["rain_p_h"] = np.round(rain_amount, 2)
    frame["snow_p_h"] = 0.0

    source_weather = frame["weather_type"].to_numpy(dtype=object)
    dry_weather = np.where(
        np.isin(source_weather, ["Clear", "Clouds", "Mist", "Haze", "Fog"]),
        source_weather, "Clouds",
    )
    weather = np.where(
        raining,
        np.where(rain_amount < 0.8, "Drizzle", np.where(rain_amount >= 6, "Thunderstorm", "Rain")),
        dry_weather,
    )
    frame["weather_type"] = weather
    descriptions = {
        "Clear": "sky is clear",
        "Clouds": "scattered clouds",
        "Mist": "mist",
        "Haze": "haze",
        "Fog": "fog",
        "Drizzle": "light intensity drizzle",
        "Rain": "moderate rain",
        "Thunderstorm": "thunderstorm with rain",
    }
    frame["weather_description"] = [descriptions[value] for value in weather]

    hcm_temperature = pd.to_numeric(frame["temperature"]).to_numpy()
    temperature = (
        273.15 + MONTHLY_MEAN_C[month - 1]
        + 2.2 * np.sin((hour - 8) * np.pi / 12)
        + 0.05 * (hcm_temperature - 301.15)
        + rng.normal(0, 1.0, count)
        - 0.7 * raining
    )
    frame["temperature"] = np.round(np.clip(temperature, 282.15, 312.15), 2)
    humidity = np.clip(
        pd.to_numeric(frame["humidity"]).to_numpy() * 0.42
        + 43 + 10 * raining + 3 * np.isin(month, [5, 6, 7, 8, 9, 10])
        + rng.normal(0, 4, count), 45, 100,
    )
    frame["humidity"] = np.rint(humidity).astype(int)
    frame["dew_point"] = np.round(
        frame["temperature"].to_numpy() - np.clip((100 - humidity) / 7, 0.5, 8), 2
    )
    source_clouds = pd.to_numeric(frame["clouds_all"]).to_numpy()
    frame["clouds_all"] = np.where(
        raining, np.maximum(source_clouds, 75),
        np.where(weather == "Clear", np.minimum(source_clouds, 20), source_clouds),
    )
    source_visibility = pd.to_numeric(frame["visibility_in_miles"]).to_numpy()
    frame["visibility_in_miles"] = np.where(
        raining, np.minimum(source_visibility, 3), source_visibility
    )

    # Retain the source's daily traffic pattern with modest Hanoi-specific
    # weekday commute peaks and independent variation.
    baseline_volume = pd.to_numeric(frame["traffic_volume"]).to_numpy() / np.where(source_rain, 0.94, 1.0)
    commute_peak = (weekday < 5) & (np.isin(hour, [7, 8, 9, 17, 18, 19]))
    daily_variation = np.repeat(rng.normal(1.0, 0.035, len(days)), 24)
    hourly_variation = np.clip(rng.normal(1.0, 0.02, count), 0.92, 1.08)
    volume = (
        baseline_volume * np.where(commute_peak, 1.07, 1.0)
        * daily_variation * hourly_variation * np.where(raining, 0.95, 1.0)
    )
    frame["traffic_volume"] = np.rint(np.clip(volume, 0, 10000)).astype(int)
    frame.to_csv(OUTPUT, index=False, lineterminator="\n")
    print(f"Wrote {len(frame):,} synthetic hourly rows to {OUTPUT}")


if __name__ == "__main__":
    main()

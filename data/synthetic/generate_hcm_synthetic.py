"""Generate a reproducible, synthetic HCMC hourly dataset for UI testing.

Traffic patterns are sampled from data/TrafficVolumeData.csv. They are not
observations from Ho Chi Minh City, and future rows are not forecasts.
"""

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "data" / "TrafficVolumeData.csv"
OUTPUT = Path(__file__).resolve().parent / "TrafficVolumeData_HCM_2015_2030_synthetic.csv"
SEED = 20261002


def source_day_pools(source: pd.DataFrame) -> dict[tuple[int, int], list[np.ndarray]]:
    """Keep complete, non-holiday source days by month and weekday."""
    pools: dict[tuple[int, int], list[np.ndarray]] = {}
    for day, positions in source.groupby(source["date_time"].dt.normalize()).indices.items():
        positions = np.asarray(positions)
        rows = source.iloc[positions]
        if len(rows) != 24 or not np.array_equal(rows["date_time"].dt.hour, np.arange(24)):
            continue
        if not rows["is_holiday"].eq("None").all():
            continue
        pools.setdefault((day.month, day.dayofweek), []).append(positions)
    return pools


def main() -> None:
    rng = np.random.default_rng(SEED)
    source = pd.read_csv(SOURCE, keep_default_na=False, parse_dates=["date_time"])
    source = source.sort_values("date_time").reset_index(drop=True)
    pools = source_day_pools(source)

    target_hours = pd.date_range("2015-01-01", "2030-12-31 23:00:00", freq="h")
    target_days = pd.date_range("2015-01-01", "2030-12-31", freq="D")
    selected = np.concatenate(
        [pools[(day.month, day.dayofweek)][rng.integers(len(pools[(day.month, day.dayofweek)]))]
         for day in target_days]
    )
    output = source.iloc[selected].copy().reset_index(drop=True)
    count = len(output)
    month = target_hours.month.to_numpy()
    hour = target_hours.hour.to_numpy()

    # A day-long source analogue retains the original hourly traffic shape.
    daily_variation = np.repeat(rng.normal(1.0, 0.04, len(target_days)), 24)
    hourly_variation = np.clip(rng.normal(1.0, 0.025, count), 0.9, 1.1)
    annual_factor = 1.0 + 0.012 * (target_hours.year.to_numpy() - 2015)

    # Fixed-date Vietnamese public holidays; Tet is omitted because its
    # calendar date varies by year. These are simulation assumptions.
    holiday_names = {
        "01-01": "New Year's Day",
        "04-30": "Reunification Day",
        "05-01": "International Workers' Day",
        "09-02": "National Day",
    }
    month_day = target_hours.strftime("%m-%d")
    output["is_holiday"] = [holiday_names.get(value, "None") for value in month_day]
    is_holiday = output["is_holiday"].ne("None").to_numpy()

    # Tropical wet season (May-November) with short afternoon/evening showers.
    wet_day = target_days.month.isin([5, 6, 7, 8, 9, 10, 11])
    rain_day = rng.random(len(target_days)) < np.where(wet_day, 0.48, 0.13)
    rain_start = rng.integers(13, 21, len(target_days))
    rain_end = rain_start + rng.integers(1, 5, len(target_days))
    hours_of_day = np.arange(24)[None, :]
    raining = (
        rain_day[:, None]
        & (hours_of_day >= rain_start[:, None])
        & (hours_of_day < rain_end[:, None])
    ).ravel()
    rain_amount = np.where(raining, rng.gamma(1.6, 1.5, count), 0.0)
    output["rain_p_h"] = np.round(rain_amount, 2)
    output["snow_p_h"] = 0.0

    original_weather = output["weather_type"].to_numpy(dtype=object)
    dry_weather = np.where(
        np.isin(original_weather, ["Clear", "Clouds", "Mist", "Haze", "Fog"]),
        original_weather,
        "Clouds",
    )
    weather = np.where(
        raining,
        np.where(rain_amount < 1.0, "Drizzle", np.where(rain_amount >= 6.0, "Thunderstorm", "Rain")),
        dry_weather,
    )
    output["weather_type"] = weather
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
    output["weather_description"] = [descriptions[value] for value in weather]

    original_temperature = pd.to_numeric(output["temperature"]).to_numpy()
    temperature = (
        301.15
        + 1.1 * np.cos((month - 4) * 2 * np.pi / 12)
        + 2.1 * np.sin((hour - 8) * np.pi / 12)
        + 0.04 * (original_temperature - 280.0)
        + rng.normal(0.0, 0.65, count)
        - 0.8 * raining
    )
    output["temperature"] = np.round(np.clip(temperature, 296.15, 309.15), 2)
    humidity = np.clip(
        pd.to_numeric(output["humidity"]).to_numpy() * 0.5
        + 39.0 + 8.0 * raining + rng.normal(0.0, 4.0, count),
        50, 100,
    )
    output["humidity"] = np.rint(humidity).astype(int)
    output["dew_point"] = np.round(
        output["temperature"].to_numpy() - np.clip((100 - humidity) / 8, 0.5, 6), 2
    )
    output["clouds_all"] = np.where(
        raining,
        np.maximum(pd.to_numeric(output["clouds_all"]).to_numpy(), 75),
        np.where(weather == "Clear", np.minimum(pd.to_numeric(output["clouds_all"]).to_numpy(), 20),
                 pd.to_numeric(output["clouds_all"]).to_numpy()),
    )
    output["visibility_in_miles"] = np.where(
        raining,
        np.minimum(pd.to_numeric(output["visibility_in_miles"]).to_numpy(), 3),
        pd.to_numeric(output["visibility_in_miles"]).to_numpy(),
    )

    source_volume = pd.to_numeric(output["traffic_volume"]).to_numpy()
    traffic = (
        source_volume * daily_variation * hourly_variation * annual_factor
        * np.where(raining, 0.94, 1.0) * np.where(is_holiday, 0.72, 1.0)
    )
    output["traffic_volume"] = np.rint(np.clip(traffic, 0, 10000)).astype(int)
    output["date_time"] = target_hours.strftime("%Y-%m-%d %H:%M:%S")
    output.to_csv(OUTPUT, index=False, lineterminator="\n")
    print(f"Wrote {len(output):,} hourly synthetic rows to {OUTPUT}")


if __name__ == "__main__":
    main()

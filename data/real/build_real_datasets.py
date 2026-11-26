"""Build real traffic datasets in the project's 15-column upload schema.

Usage (from the repository root):
    python data/real/build_real_datasets.py fetch   # download sources into data/real/*/source/
    python data/real/build_real_datasets.py build   # write the 15-column CSV files
    python data/real/build_real_datasets.py check   # run the website's dataset validator offline

Traffic counts are never invented: hours without a count stay absent, and the
training pipeline marks them as unobserved. Weather comes from Open-Meteo
(ERA5 reanalysis); see data/README.md for sources, licences and limits.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
COLUMNS = [
    "date_time", "is_holiday", "air_pollution_index", "humidity", "wind_speed",
    "wind_direction", "visibility_in_miles", "dew_point", "temperature", "rain_p_h",
    "snow_p_h", "clouds_all", "weather_type", "weather_description", "traffic_volume",
]
# Columns with no real source for a dataset are filled with 0 and documented.
NO_DATA = 0

I94 = HERE / "i94-minneapolis"
GLASGOW = HERE / "glasgow"
GLASGOW_SENSORS = ("GD1351_R", "GD1501_A", "GD0801_A")

UCI_URL = "https://archive.ics.uci.edu/static/public/492/metro+interstate+traffic+volume.zip"
GLASGOW_URL = "https://zenodo.org/records/14537113/files/traffic_flow_data.zip?download=1"
GLASGOW_ZIP_MD5 = "fe7bcce6abc8b69046cbff28cf466991"
BANK_HOLIDAYS_URL = "https://www.gov.uk/bank-holidays.json"
WEATHER_VARIABLES = ("temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m,"
                     "wind_direction_10m,rain,snowfall,cloud_cover,weather_code")
# Approximate midpoint of I-94 between Minneapolis and St Paul (MnDOT ATR 301 is
# "roughly midway"); one ERA5 cell (~25 km) covers the whole corridor.
I94_POINT = (44.96, -93.18)
I94_PERIOD = ("2012-10-01", "2018-10-01")
GLASGOW_PERIOD = ("2019-10-01", "2023-09-30")


def open_meteo_weather_url(point, period):
    return ("https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={point[0]}&longitude={point[1]}&start_date={period[0]}&end_date={period[1]}"
            f"&hourly={WEATHER_VARIABLES}&wind_speed_unit=mph&timezone=GMT&format=csv")


def open_meteo_air_url(point, period):
    return ("https://air-quality-api.open-meteo.com/v1/air-quality"
            f"?latitude={point[0]}&longitude={point[1]}&start_date={period[0]}&end_date={period[1]}"
            "&hourly=european_aqi,pm2_5,pm10,nitrogen_dioxide&domains=cams_europe&timezone=GMT&format=csv")


# WMO weather codes (Open-Meteo) mapped to the project's OpenWeather-style labels.
WMO_LABELS = {
    0: ("Clear", "sky is clear"), 1: ("Clouds", "few clouds"), 2: ("Clouds", "scattered clouds"),
    3: ("Clouds", "overcast clouds"), 45: ("Fog", "fog"), 48: ("Fog", "fog"),
    51: ("Drizzle", "light intensity drizzle"), 53: ("Drizzle", "drizzle"),
    55: ("Drizzle", "heavy intensity drizzle"), 56: ("Drizzle", "freezing drizzle"),
    57: ("Drizzle", "freezing drizzle"), 61: ("Rain", "light rain"), 63: ("Rain", "moderate rain"),
    65: ("Rain", "heavy intensity rain"), 66: ("Rain", "freezing rain"), 67: ("Rain", "freezing rain"),
    71: ("Snow", "light snow"), 73: ("Snow", "snow"), 75: ("Snow", "heavy snow"), 77: ("Snow", "snow grains"),
    80: ("Rain", "light intensity shower rain"), 81: ("Rain", "shower rain"),
    82: ("Rain", "heavy intensity shower rain"), 85: ("Snow", "light shower snow"),
    86: ("Snow", "heavy shower snow"), 95: ("Thunderstorm", "thunderstorm"),
    96: ("Thunderstorm", "thunderstorm with hail"), 99: ("Thunderstorm", "thunderstorm with heavy hail"),
}


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "traffic-dataset-builder/1.0"})
    with urllib.request.urlopen(request, timeout=600) as response:
        return response.read()


def write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    print(f"  {path.relative_to(ROOT)}  {len(content):,} bytes  sha256 {hashlib.sha256(content).hexdigest()[:12]}")


def fetch() -> None:
    print("UCI Metro Interstate Traffic Volume")
    with zipfile.ZipFile(io.BytesIO(download(UCI_URL))) as archive:
        write(I94 / "source" / "Metro_Interstate_Traffic_Volume.csv.gz",
              archive.read("Metro_Interstate_Traffic_Volume.csv.gz"))
    write(I94 / "source" / "open_meteo_era5_utc.csv", download(open_meteo_weather_url(I94_POINT, I94_PERIOD)))

    print("Glasgow traffic flow")
    content = download(GLASGOW_URL)
    if hashlib.md5(content).hexdigest() != GLASGOW_ZIP_MD5:
        raise SystemExit("Glasgow archive checksum does not match the Zenodo record.")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        write(GLASGOW / "source" / "locations.csv", archive.read("locations.csv"))
        write(GLASGOW / "source" / "status.csv", archive.read("status.csv"))
        for sensor in GLASGOW_SENSORS:
            write(GLASGOW / "source" / "flows" / f"{sensor}.csv", archive.read(f"flows/{sensor}.csv"))
    point = glasgow_point()
    write(GLASGOW / "source" / "open_meteo_era5_utc.csv", download(open_meteo_weather_url(point, GLASGOW_PERIOD)))
    write(GLASGOW / "source" / "open_meteo_cams_utc.csv", download(open_meteo_air_url(point, GLASGOW_PERIOD)))
    write(GLASGOW / "source" / "uk_bank_holidays.json", download(BANK_HOLIDAYS_URL))


def glasgow_point():
    locations = pd.read_csv(GLASGOW / "source" / "locations.csv").set_index("id").loc[list(GLASGOW_SENSORS)]
    return round(float(locations.latitude.mean()), 3), round(float(locations.longitude.mean()), 3)


def read_open_meteo(path: Path) -> pd.DataFrame:
    """Open-Meteo CSV: three metadata lines, then hourly rows in UTC."""
    frame = pd.read_csv(path, skiprows=3)
    frame.columns = [column.split(" (")[0] for column in frame.columns]
    frame["time"] = pd.to_datetime(frame["time"])
    if frame.isna().any().any():
        raise SystemExit(f"{path.name} contains missing values.")
    return frame.set_index("time")


def weather_columns(weather: pd.DataFrame, utc_times: pd.Series) -> pd.DataFrame:
    rows = weather.reindex(utc_times.to_numpy())
    if rows.isna().any().any():
        raise SystemExit("Weather does not cover every traffic hour.")
    rows = rows.reset_index(drop=True)
    return pd.DataFrame({
        "humidity": rows.relative_humidity_2m.round().astype(int),
        "wind_speed": rows.wind_speed_10m.round(1),
        "wind_direction": rows.wind_direction_10m.round().astype(int),
        "dew_point": (rows.dew_point_2m + 273.15).round(2),
        "temperature": (rows.temperature_2m + 273.15).round(2),
        "rain_p_h": rows.rain.round(2),
        "snow_p_h": (rows.snowfall * 10).round(2),  # cm of snow -> mm
        "clouds_all": rows.cloud_cover.round().astype(int),
        "weather_code": rows.weather_code.astype(int),
    })


def build_i94() -> dict:
    uci = pd.read_csv(I94 / "source" / "Metro_Interstate_Traffic_Volume.csv.gz", keep_default_na=False)
    uci["date_time"] = pd.to_datetime(uci["date_time"])
    duplicates = int(uci.date_time.duplicated().sum())
    # Duplicate hours share one count and differ only by weather label; keep the first.
    uci = uci.drop_duplicates("date_time").sort_values("date_time").reset_index(drop=True)
    # UCI times are local wall-clock (America/Chicago with DST; checked against ERA5).
    local = uci.date_time.dt.tz_localize("America/Chicago", ambiguous=True, nonexistent="shift_forward")
    utc = local.dt.tz_convert("UTC").dt.tz_localize(None)
    weather = weather_columns(read_open_meteo(I94 / "source" / "open_meteo_era5_utc.csv"), utc)

    temperature = uci.temp.astype(float)
    bad_temperature = temperature < 200  # 0 K sensor errors
    rain = uci.rain_1h.astype(float)
    bad_rain = rain > 100  # impossible hourly totals (up to 9,831 mm)
    holiday_by_day = {day: name for day, name in zip(uci.date_time.dt.date, uci.holiday) if name != "None"}
    output = pd.DataFrame({
        "date_time": uci.date_time.dt.strftime("%Y-%m-%d %H:%M:%S"),
        "is_holiday": [holiday_by_day.get(day, "None") for day in uci.date_time.dt.date],
        "air_pollution_index": NO_DATA,
        "humidity": weather.humidity,
        "wind_speed": weather.wind_speed,
        "wind_direction": weather.wind_direction,
        "visibility_in_miles": NO_DATA,
        "dew_point": weather.dew_point,
        "temperature": np.where(bad_temperature, weather.temperature, temperature).round(2),
        "rain_p_h": np.where(bad_rain, weather.rain_p_h, rain).round(2),
        "snow_p_h": uci.snow_1h.astype(float).round(2),
        "clouds_all": uci.clouds_all.astype(int),
        "weather_type": uci.weather_main,
        "weather_description": uci.weather_description,
        "traffic_volume": uci.traffic_volume.astype(int),
    })[COLUMNS]
    path = I94 / "TrafficVolumeData_I94_Minneapolis_2012_2018.csv"
    output.to_csv(path, index=False, lineterminator="\n")
    return {"file": path, "rows": len(output), "duplicates_removed": duplicates,
            "temperature_replaced": int(bad_temperature.sum()), "rain_replaced": int(bad_rain.sum()),
            "holiday_days": len(holiday_by_day)}


def build_glasgow() -> list[dict]:
    weather = read_open_meteo(GLASGOW / "source" / "open_meteo_era5_utc.csv")
    air = read_open_meteo(GLASGOW / "source" / "open_meteo_cams_utc.csv")
    holidays = json.loads((GLASGOW / "source" / "uk_bank_holidays.json").read_text(encoding="utf-8"))
    holiday_by_day = {pd.Timestamp(event["date"]).date(): event["title"].replace("’", "'")
                      for event in holidays["scotland"]["events"]}
    results = []
    for sensor in GLASGOW_SENSORS:
        flows = pd.read_csv(GLASGOW / "source" / "flows" / f"{sensor}.csv")
        # Zenodo files hold UTC hours (evening peak moves one hour earlier in summer).
        times = pd.to_datetime(flows.date) + pd.to_timedelta(flows.time, unit="h")
        flows = pd.DataFrame({"utc": times, "flow": flows.flow})
        flows = flows[(flows.utc >= GLASGOW_PERIOD[0]) & (flows.utc < "2023-10-01")]
        duplicates = int(flows.utc.duplicated().sum())
        flows = flows.drop_duplicates("utc").sort_values("utc").reset_index(drop=True)
        rows = weather_columns(weather, flows.utc)
        aqi = air.european_aqi.reindex(flows.utc.to_numpy())
        if aqi.isna().any():
            raise SystemExit("Air quality does not cover every traffic hour.")
        local_day = flows.utc.dt.tz_localize("UTC").dt.tz_convert("Europe/London").dt.date
        labels = [WMO_LABELS[code] for code in rows.weather_code]
        output = pd.DataFrame({
            "date_time": flows.utc.dt.strftime("%Y-%m-%d %H:%M:%S"),
            "is_holiday": [holiday_by_day.get(day, "None") for day in local_day],
            "air_pollution_index": aqi.round().astype(int).to_numpy(),
            "humidity": rows.humidity,
            "wind_speed": rows.wind_speed,
            "wind_direction": rows.wind_direction,
            "visibility_in_miles": NO_DATA,
            "dew_point": rows.dew_point,
            "temperature": rows.temperature,
            "rain_p_h": rows.rain_p_h,
            "snow_p_h": rows.snow_p_h,
            "clouds_all": rows.clouds_all,
            "weather_type": [label[0] for label in labels],
            "weather_description": [label[1] for label in labels],
            "traffic_volume": flows.flow.round().astype(int),
        })[COLUMNS]
        path = GLASGOW / f"TrafficVolumeData_Glasgow_{sensor}_2019_2023.csv"
        output.to_csv(path, index=False, lineterminator="\n")
        results.append({"file": path, "rows": len(output), "duplicates_removed": duplicates,
                        "holiday_hours": int((output.is_holiday != "None").sum())})
    return results


def outputs() -> list[Path]:
    return [I94 / "TrafficVolumeData_I94_Minneapolis_2012_2018.csv",
            *(GLASGOW / f"TrafficVolumeData_Glasgow_{sensor}_2019_2023.csv" for sensor in GLASGOW_SENSORS)]


def build() -> None:
    for result in [build_i94(), *build_glasgow()]:
        path = result.pop("file")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        print(f"{path.relative_to(ROOT)}  sha256 {digest}  {result}")


def check() -> None:
    sys.path.insert(0, str(ROOT))
    from backend.app.services.datasets import validate_training_csv
    for path in outputs():
        result = validate_training_csv(path.read_bytes())
        print(f"{path.name}: valid, {result.row_count:,} rows, {result.start_at} .. {result.end_at}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("fetch", "build", "check"))
    {"fetch": fetch, "build": build, "check": check}[parser.parse_args().command]()

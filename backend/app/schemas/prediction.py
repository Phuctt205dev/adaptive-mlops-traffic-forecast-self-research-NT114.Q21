import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.app.db.models import ForecastMode


class PredictionCreate(BaseModel):
    forecast_for: datetime
    forecast_mode: ForecastMode = ForecastMode.FUTURE_TIME
    air_pollution_index: float | None = Field(default=None, ge=0)
    humidity: float | None = Field(default=None, ge=0, le=100)
    wind_speed: float | None = Field(default=None, ge=0)
    wind_direction: float | None = Field(default=None, ge=0, le=360)
    visibility_in_miles: float | None = Field(default=None, ge=0)
    dew_point: float | None = None
    temperature: float | None = None
    rain_p_h: float | None = Field(default=None, ge=0)
    snow_p_h: float | None = Field(default=None, ge=0)
    clouds_all: float | None = Field(default=None, ge=0, le=100)
    weather_type: str | None = None
    weather_description: str | None = None
    is_holiday: str | None = None


class PredictionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    region_id: uuid.UUID
    model_version_id: uuid.UUID
    forecast_for: datetime
    forecast_mode: ForecastMode
    prediction: float
    feature_snapshot_json: dict
    created_at: datetime
    updated_at: datetime


class PredictionResponse(BaseModel):
    prediction: PredictionRead
    model_version: str
    model_variant: str
    active_model_version_id: uuid.UUID


class PredictionWindowResponse(BaseModel):
    region_id: uuid.UUID
    active_model_version_id: uuid.UUID
    model_version: str
    model_variant: str
    dataset_id: uuid.UUID
    dataset_filename: str
    production_start_at: datetime
    production_end_at: datetime
    available_points: int


class ForecastPoint(BaseModel):
    forecast_for: datetime
    prediction: float


class DailyForecast(BaseModel):
    date: datetime
    min_prediction: float
    max_prediction: float
    avg_prediction: float
    points: list[ForecastPoint]


class ForecastDashboardResponse(BaseModel):
    region_id: uuid.UUID
    active_model_version_id: uuid.UUID
    model_version: str
    model_variant: str
    generated_at: datetime
    hourly_24h: list[ForecastPoint]
    daily_7d: list[DailyForecast]

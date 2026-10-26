import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class QualityPoint(BaseModel):
    forecast_for: datetime
    reference: float | None
    prediction: float | None
    absolute_error: float | None
    label_source: Literal["observed", "synthetic", "unknown", "missing"]


class QualityMetrics(BaseModel):
    mae: float | None
    rmse: float | None


class QualityCoverage(BaseModel):
    requested_points: int
    dataset_points: int
    predicted_points: int
    paired_points: int
    observed_labels: int
    synthetic_labels: int
    unknown_labels: int


class ModelQualityReport(BaseModel):
    region_id: uuid.UUID
    active_model_version_id: uuid.UUID
    model_version: str
    model_variant: str
    dataset_id: uuid.UUID
    dataset_filename: str
    timezone: str
    mode: Literal["reference", "observed"]
    evaluation_policy: str
    generated_at: datetime
    start_at: datetime
    end_at: datetime
    available_start_at: datetime
    available_end_at: datetime
    training_end_at: datetime
    metrics: QualityMetrics
    coverage: QualityCoverage
    warnings: list[str]
    points: list[QualityPoint]

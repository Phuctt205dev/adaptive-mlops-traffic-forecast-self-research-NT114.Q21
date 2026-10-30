import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FeatureInfluence(BaseModel):
    label: str
    mean_abs_shap: float = Field(ge=0, allow_inf_nan=False)
    share_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    features: list[str]


class FeatureImportanceReport(BaseModel):
    region_id: uuid.UUID
    active_model_version_id: uuid.UUID | None
    model_version: str | None
    model_variant: str | None
    dataset_filename: str | None
    status: Literal["ready", "pending", "unavailable", "unsupported"]
    message: str | None
    generated_at: datetime | None
    sample_count: int
    transformed_feature_count: int
    features: list[FeatureInfluence]
    explanation_method: Literal["TreeExplainer", "GradientExplainer"] | None = None
    sequence_length: int | None = Field(default=None, ge=1)
    input_features: list[str] = []
    model_reports: list["AlgorithmFeatureReport"] = []
    model_average: "ModelAverage | None" = None


class AverageFeatureInfluence(BaseModel):
    label: str
    share_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    features: list[str]
    reports_present: int = Field(ge=1)


class AlgorithmFeatureReport(BaseModel):
    model_name: str
    status: Literal["ready", "pending", "unavailable", "unsupported"]
    message: str | None
    features: list[FeatureInfluence]
    input_features: list[str] = []
    sample_count: int = Field(ge=0)
    generated_at: datetime | None
    sequence_length: int | None
    explanation_method: str | None
    is_serving: bool


class ModelAverage(BaseModel):
    status: Literal["ready", "incomplete"]
    used_reports: int = Field(ge=0, le=5)
    total_models: Literal[5]
    features: list[AverageFeatureInfluence]


FeatureImportanceReport.model_rebuild()

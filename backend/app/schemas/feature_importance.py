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
    history: "FeatureImportanceHistory | None" = None


class AverageFeatureInfluence(BaseModel):
    label: str
    share_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    features: list[str]
    reports_present: int = Field(ge=1)


class SavedModelReport(BaseModel):
    model_version_id: uuid.UUID
    model_version: str
    model_variant: str
    dataset_filename: str | None
    status: Literal["included", "missing", "unavailable", "unsupported", "invalid", "zero"]


class ExcludedReports(BaseModel):
    missing: int = Field(ge=0)
    unavailable: int = Field(ge=0)
    unsupported: int = Field(ge=0)
    invalid: int = Field(ge=0)
    zero: int = Field(ge=0)


class FeatureImportanceHistory(BaseModel):
    status: Literal["ready", "pending"]
    total_models: int = Field(ge=0)
    used_reports: int = Field(ge=0)
    excluded_reports: ExcludedReports
    dataset_count: int = Field(ge=0)
    features: list[AverageFeatureInfluence]
    models: list[SavedModelReport]


FeatureImportanceReport.model_rebuild()

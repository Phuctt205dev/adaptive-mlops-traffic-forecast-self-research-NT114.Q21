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
    active_model_version_id: uuid.UUID
    model_version: str
    model_variant: str
    dataset_filename: str
    status: Literal["ready", "pending", "unavailable", "unsupported"]
    message: str | None
    generated_at: datetime | None
    sample_count: int
    transformed_feature_count: int
    features: list[FeatureInfluence]
    explanation_method: Literal["TreeExplainer", "GradientExplainer"] | None = None
    sequence_length: int | None = Field(default=None, ge=1)

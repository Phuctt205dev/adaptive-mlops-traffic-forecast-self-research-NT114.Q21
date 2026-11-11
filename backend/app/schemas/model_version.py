import uuid
from datetime import date, datetime
from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.db.models import ModelVersionStatus, TrainingRunStatus
from src.shap_config import resolve_shap_config


class TrainingRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    region_id: uuid.UUID
    dataset_id: uuid.UUID
    airflow_dag_run_id: str | None
    status: TrainingRunStatus
    split_policy: str
    configuration_json: dict
    recommended_model_version_id: uuid.UUID | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    requested_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ModelVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    region_id: uuid.UUID
    training_run_id: uuid.UUID
    variant: str
    version: str
    mlflow_run_id: str
    mlflow_model_uri: str
    artifact_uri: str
    cv_mean_mae: float
    cv_std_mae: float
    final_test_mae: float
    status: ModelVersionStatus
    created_at: datetime
    updated_at: datetime
    training_configuration: dict = Field(default_factory=dict)
    model_comparison: list[dict] = Field(default_factory=list)


class ModelVersionList(BaseModel):
    items: list[ModelVersionRead]
    page: int
    page_size: int
    total: int


class ModelActivationResponse(BaseModel):
    active_model_version: ModelVersionRead


class TrainingRunCreate(BaseModel):
    training_backend: Literal["kaggle"] = "kaggle"
    train_start_date: str
    train_end_date: str
    artifact_root: str = "models/regions"
    model_role: str = "candidate"
    cv_splits: int = Field(default=3, ge=2)
    random_state: int = Field(default=42, ge=0, le=4294967295)
    selected_models: list[str] = Field(
        default_factory=lambda: [
            "random_forest_lag",
            "xgboost_lag",
            "lightgbm_lag",
            "lstm",
            "gru",
        ]
    )
    recurrent_sequence_length: int = Field(default=72, ge=1)
    recurrent_epochs: int = Field(default=3, ge=1)
    recurrent_batch_size: int = Field(default=32, ge=1)
    final_test_ratio: float = Field(default=0.15, gt=0, lt=0.5)
    trigger_source: str | None = None
    shap: dict = Field(default_factory=dict)
    drift_current_end: str | None = None
    retrain_source_model_id: uuid.UUID | None = None

    @field_validator("train_start_date", "train_end_date")
    @classmethod
    def validate_training_date(cls, value):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Use a date in YYYY-MM-DD format.")
        return value

    @model_validator(mode="after")
    def validate_training_window(self):
        if self.train_start_date >= self.train_end_date:
            raise ValueError("Training start date must be before the end date.")
        return self

    @field_validator("shap")
    @classmethod
    def validate_shap(cls, value):
        return asdict(resolve_shap_config(value))

    @field_validator("selected_models")
    @classmethod
    def validate_selected_models(cls, value):
        supported = {
            "random_forest_lag",
            "xgboost_lag",
            "lightgbm_lag",
            "lstm",
            "gru",
        }
        normalized = []
        for model in value or []:
            item = str(model).strip().lower()
            if item not in supported:
                raise ValueError(f"Unsupported model variant: {model}.")
            if item not in normalized:
                normalized.append(item)
        if not normalized:
            raise ValueError("Select at least one model to train.")
        return normalized


class TrainingRunTriggerResponse(BaseModel):
    training_run: TrainingRunRead
    dag_id: str
    dag_run_id: str
    airflow_response: dict

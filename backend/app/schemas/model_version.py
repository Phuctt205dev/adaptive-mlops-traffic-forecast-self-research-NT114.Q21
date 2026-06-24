import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.db.models import ModelVersionStatus, TrainingRunStatus


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
    train_start_date: str
    train_end_date: str
    artifact_root: str = "models/regions"
    model_role: str = "candidate"
    cv_splits: int = 3
    random_state: int = 42
    selected_models: list[str] = Field(
        default_factory=lambda: [
            "random_forest_lag",
            "xgboost_lag",
            "lightgbm_lag",
            "lstm",
            "gru",
        ]
    )
    recurrent_sequence_length: int = 72
    recurrent_epochs: int = 3
    recurrent_batch_size: int = 32
    final_test_ratio: float = 0.15
    trigger_source: str | None = None

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

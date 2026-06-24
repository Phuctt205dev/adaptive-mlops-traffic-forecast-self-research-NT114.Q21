import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class DatasetStatus(str, enum.Enum):
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    VALIDATION_FAILED = "validation_failed"
    VALID = "valid"
    PROCESSING = "processing"
    READY = "ready"
    ARCHIVED = "archived"


class TrainingRunStatus(str, enum.Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    PREPROCESSING = "preprocessing"
    TRAINING = "training"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ModelVersionStatus(str, enum.Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    ARCHIVED = "archived"
    FAILED = "failed"


class ForecastMode(str, enum.Enum):
    NEXT_HOUR = "next_hour"
    FUTURE_TIME = "future_time"


class DriftCheckStatus(str, enum.Enum):
    STABLE = "stable"
    DRIFT_DETECTED = "drift_detected"
    RETRAIN_TRIGGERED = "retrain_triggered"
    SKIPPED = "skipped"
    FAILED = "failed"


def enum_values(enum_class):
    return [member.value for member in enum_class]


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(160))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=enum_values),
        default=UserRole.USER,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Region(TimestampMixin, Base):
    __tablename__ = "regions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    active_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "model_versions.id",
            name="fk_regions_active_model_version",
            use_alter=True,
        )
    )


class Dataset(TimestampMixin, Base):
    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint("region_id", "sha256", name="uq_datasets_region_sha256"),
        Index("ix_datasets_region_created_at", "region_id", "created_at"),
        Index("ix_datasets_region_status", "region_id", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    region_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regions.id"), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dvc_rev: Mapped[str | None] = mapped_column(String(255))
    row_count: Mapped[int | None] = mapped_column(Integer)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[DatasetStatus] = mapped_column(
        Enum(DatasetStatus, name="dataset_status", values_callable=enum_values),
        default=DatasetStatus.UPLOADED,
        nullable=False,
    )
    quality_report_uri: Mapped[str | None] = mapped_column(Text)
    validation_error: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)


class TrainingRun(TimestampMixin, Base):
    __tablename__ = "training_runs"
    __table_args__ = (
        Index("ix_training_runs_region_created_at", "region_id", "created_at"),
        Index("ix_training_runs_status", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    region_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regions.id"), nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"), nullable=False)
    airflow_dag_run_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[TrainingRunStatus] = mapped_column(
        Enum(
            TrainingRunStatus,
            name="training_run_status",
            values_callable=enum_values,
        ),
        default=TrainingRunStatus.QUEUED,
        nullable=False,
    )
    split_policy: Mapped[str] = mapped_column(String(100), nullable=False)
    configuration_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    recommended_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "model_versions.id",
            name="fk_training_runs_recommended_model",
            use_alter=True,
        )
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)


class ModelVersion(TimestampMixin, Base):
    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("region_id", "version", name="uq_model_versions_region_version"),
        Index("ix_model_versions_region_status", "region_id", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    region_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regions.id"), nullable=False)
    training_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("training_runs.id"), nullable=False
    )
    variant: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    mlflow_run_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    mlflow_model_uri: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_uri: Mapped[str] = mapped_column(Text, nullable=False)
    cv_mean_mae: Mapped[float] = mapped_column(Float, nullable=False)
    cv_std_mae: Mapped[float] = mapped_column(Float, nullable=False)
    final_test_mae: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[ModelVersionStatus] = mapped_column(
        Enum(
            ModelVersionStatus,
            name="model_version_status",
            values_callable=enum_values,
        ),
        default=ModelVersionStatus.CANDIDATE,
        nullable=False,
    )


class Prediction(TimestampMixin, Base):
    __tablename__ = "predictions"
    __table_args__ = (
        Index("ix_predictions_user_created_at", "user_id", "created_at"),
        Index("ix_predictions_region_created_at", "region_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    region_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regions.id"), nullable=False)
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id"), nullable=False
    )
    forecast_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    forecast_mode: Mapped[ForecastMode] = mapped_column(
        Enum(ForecastMode, name="forecast_mode", values_callable=enum_values),
        nullable=False,
    )
    prediction: Mapped[float] = mapped_column(Float, nullable=False)
    feature_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)


class DriftCheck(Base):
    __tablename__ = "drift_checks"
    __table_args__ = (
        Index("ix_drift_checks_region_created_at", "region_id", "created_at"),
        Index("ix_drift_checks_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    region_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("regions.id"), nullable=False)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("datasets.id"))
    active_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id")
    )
    reference_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reference_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[DriftCheckStatus] = mapped_column(
        Enum(DriftCheckStatus, name="drift_check_status", values_callable=enum_values),
        nullable=False,
    )
    drift_detected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    drifted_feature_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    feature_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    feature_drift_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    triggered_training_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("training_runs.id")
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    before_json: Mapped[dict | None] = mapped_column(JSON)
    after_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

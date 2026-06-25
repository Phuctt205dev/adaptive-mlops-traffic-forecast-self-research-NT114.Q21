from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Traffic Management API"
    api_v1_prefix: str = "/api/v1"
    database_url: str = (
        "postgresql+psycopg://traffic:traffic_dev_password"
        "@localhost:5432/traffic_management"
    )
    minio_health_url: str = "http://localhost:9000/minio/health/live"
    mlflow_health_url: str = "http://localhost:5000/health"
    external_health_timeout_seconds: float = 2.0
    s3_endpoint_url: str = "http://localhost:9000"
    aws_access_key_id: str = "minioadmin"
    aws_secret_access_key: str = "minioadmin123"
    aws_default_region: str = "us-east-1"
    dataset_bucket: str = "traffic-datasets"
    dataset_contract_version: str = "traffic-training-csv/v1"
    dataset_max_upload_bytes: int = 100 * 1024 * 1024
    dataset_min_rows: int = 4380
    dvc_enabled: bool = True
    dvc_required: bool = False
    dvc_remote_name: str = "minio"
    dvc_remote_url: str = "s3://dvc-storage"
    dvc_remote_endpoint: str = "http://minio:9000"
    dvc_repo_root: str = "."
    dvc_workspace_dir: str = "data/dvc_data"
    dvc_keep_local_snapshot: bool = False
    airflow_api_url: str = "http://localhost:8080/api/v1"
    airflow_admin_username: str = "admin"
    airflow_admin_password: str = "admin"
    training_dag_id: str = "traffic_region_training"
    drift_dag_id: str = "traffic_region_drift_monitoring"
    internal_training_token: str = "local-internal-training-token"
    drift_window_days: int = 7
    drift_reference_days: int = 30
    drift_min_window_rows: int = 168
    drift_psi_threshold: float = 0.2
    drift_js_threshold: float = 0.15
    drift_min_drifted_features: int = 2
    drift_auto_retrain: bool = True
    drift_auto_promote: bool = False
    drift_retrain_window_months: int = 24
    drift_retrain_cooldown_hours: int = 72
    drift_max_regions_per_run: int = 1
    auth_secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 60 * 24
    auth_rate_limit_per_minute: int = 20
    prediction_rate_limit_per_minute: int = 120
    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "admin123456"
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:30073",
        "http://127.0.0.1:30073",
    ]


@lru_cache
def get_settings() -> Settings:
    return Settings()

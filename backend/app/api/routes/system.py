from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.api.dependencies import require_admin
from backend.app.api.routes.health import _database_health, _http_health
from backend.app.core.config import get_settings
from backend.app.db.models import (
    Dataset,
    ModelVersion,
    Prediction,
    Region,
    TrainingRun,
    User,
)
from backend.app.db.session import get_db


router = APIRouter(dependencies=[Depends(require_admin)])


def _count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def _count_by_status(db: Session, model, status_column) -> dict[str, int]:
    rows = db.execute(
        select(status_column, func.count()).select_from(model).group_by(status_column)
    ).all()
    return {str(status.value if hasattr(status, "value") else status): count for status, count in rows}


@router.get("/system/summary")
def system_summary(db: Session = Depends(get_db)):
    settings = get_settings()
    dataset_statuses = _count_by_status(db, Dataset, Dataset.status)
    training_statuses = _count_by_status(db, TrainingRun, TrainingRun.status)
    model_statuses = _count_by_status(db, ModelVersion, ModelVersion.status)

    recent_training_runs = list(
        db.execute(
            select(
                TrainingRun.id,
                TrainingRun.region_id,
                TrainingRun.dataset_id,
                TrainingRun.status,
                TrainingRun.airflow_dag_run_id,
                TrainingRun.created_at,
                TrainingRun.completed_at,
                TrainingRun.error_message,
            )
            .order_by(TrainingRun.created_at.desc())
            .limit(5)
        )
    )
    recent_predictions = list(
        db.execute(
            select(
                Prediction.id,
                Prediction.region_id,
                Prediction.model_version_id,
                Prediction.prediction,
                Prediction.forecast_for,
                Prediction.created_at,
            )
            .order_by(Prediction.created_at.desc())
            .limit(5)
        )
    )

    dependencies = {
        "database": _database_health(),
        "minio": _http_health(
            settings.minio_health_url,
            settings.external_health_timeout_seconds,
        ),
        "mlflow": _http_health(
            settings.mlflow_health_url,
            settings.external_health_timeout_seconds,
        ),
    }
    dependency_status = (
        "healthy"
        if all(item["status"] == "healthy" for item in dependencies.values())
        else "degraded"
    )

    return {
        "status": dependency_status,
        "totals": {
            "users": _count(db, User),
            "regions": _count(db, Region),
            "datasets": _count(db, Dataset),
            "training_runs": _count(db, TrainingRun),
            "model_versions": _count(db, ModelVersion),
            "predictions": _count(db, Prediction),
        },
        "datasets_by_status": dataset_statuses,
        "training_runs_by_status": training_statuses,
        "model_versions_by_status": model_statuses,
        "dependencies": dependencies,
        "recent_training_runs": [
            {
                "id": str(row.id),
                "region_id": str(row.region_id),
                "dataset_id": str(row.dataset_id),
                "status": row.status.value,
                "airflow_dag_run_id": row.airflow_dag_run_id,
                "created_at": row.created_at,
                "completed_at": row.completed_at,
                "error_message": row.error_message,
            }
            for row in recent_training_runs
        ],
        "recent_predictions": [
            {
                "id": str(row.id),
                "region_id": str(row.region_id),
                "model_version_id": str(row.model_version_id),
                "prediction": row.prediction,
                "forecast_for": row.forecast_for,
                "created_at": row.created_at,
            }
            for row in recent_predictions
        ],
    }

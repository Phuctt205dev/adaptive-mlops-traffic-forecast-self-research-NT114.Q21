import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.api.dependencies import require_admin
from backend.app.core.config import get_settings
from backend.app.core.errors import ApplicationError
from backend.app.db.models import Region
from backend.app.db.session import get_db
from backend.app.services import airflow_client, drift_monitoring


router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/regions/{region_id}/drift-checks")
def list_region_drift_checks(
    region_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    try:
        return drift_monitoring.list_region_drift_checks(
            db,
            region_id,
            limit=limit,
        )
    except ValueError as error:
        raise ApplicationError("drift_region_invalid", str(error), 404) from error


@router.post("/regions/{region_id}/drift-checks/run")
def run_region_drift_check(
    region_id: uuid.UUID,
    auto_retrain: bool = Query(default=False),
    force_retrain: bool = Query(default=False),
    current_end: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if db.get(Region, region_id) is None:
        raise ApplicationError("drift_region_invalid", "Region was not found.", 404)

    settings = get_settings()
    dag_run_id = f"manual-drift-{region_id}-{uuid.uuid4()}"
    conf = {
        "region_id": str(region_id),
        "auto_retrain": auto_retrain,
        "force_retrain": force_retrain,
        "trigger_source": "admin_ui",
    }
    if current_end:
        conf["current_end"] = current_end.isoformat()

    airflow_response = airflow_client.trigger_dag(
        settings.drift_dag_id,
        conf,
        dag_run_id=dag_run_id,
    )
    return {
        "dag_id": settings.drift_dag_id,
        "dag_run_id": dag_run_id,
        "conf": conf,
        "airflow_response": airflow_response,
    }

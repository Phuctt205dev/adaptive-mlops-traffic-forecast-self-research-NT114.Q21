import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.api.dependencies import require_admin
from backend.app.core.errors import ApplicationError
from backend.app.db.session import get_db
from backend.app.services import drift_monitoring


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
    db: Session = Depends(get_db),
):
    try:
        return drift_monitoring.check_drift_for_region(
            db,
            region_id,
            auto_retrain=auto_retrain,
            force_retrain=force_retrain,
        )
    except ValueError as error:
        raise ApplicationError("drift_region_invalid", str(error), 404) from error

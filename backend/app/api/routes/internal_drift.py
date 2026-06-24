import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.errors import ApplicationError
from backend.app.db.session import get_db
from backend.app.services import drift_monitoring


router = APIRouter()


def require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    if x_internal_token != get_settings().internal_training_token:
        raise ApplicationError("internal_token_invalid", "Invalid internal token.", 403)


@router.post("/drift/check")
def check_drift(
    auto_retrain: bool = Query(default=True),
    max_regions: int | None = Query(default=None, ge=1),
    current_end: datetime | None = Query(default=None),
    _token: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
):
    return drift_monitoring.check_drift_for_regions(
        db,
        auto_retrain=auto_retrain,
        max_regions=max_regions,
        current_end_at=current_end,
        check_method="auto",
    )


@router.post("/drift/check/regions/{region_id}")
def check_region_drift(
    region_id: uuid.UUID,
    auto_retrain: bool = Query(default=True),
    force_retrain: bool = Query(default=False),
    current_end: datetime | None = Query(default=None),
    _token: None = Depends(require_internal_token),
    db: Session = Depends(get_db),
):
    try:
        return drift_monitoring.check_drift_for_region(
            db,
            region_id,
            auto_retrain=auto_retrain,
            force_retrain=force_retrain,
            current_end_at=current_end,
            check_method="auto",
        )
    except ValueError as error:
        raise ApplicationError("region_not_found", str(error), 404) from error

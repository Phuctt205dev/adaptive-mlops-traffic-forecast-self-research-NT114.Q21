import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from backend.app.api.dependencies import require_admin
from backend.app.db.session import get_db
from backend.app.schemas.model_quality import ModelQualityReport
from backend.app.services.model_quality import get_model_quality


router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/regions/{region_id}/model-quality", response_model=ModelQualityReport)
def read_model_quality(
    region_id: uuid.UUID,
    request: Request,
    mode: Literal["reference", "observed"] = "observed",
    window: Literal["recent", "replay"] = "recent",
    start_at: datetime | None = None,
    hours: int = Query(default=24, ge=1, le=168),
    model_version_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
):
    return get_model_quality(
        db, region_id, mode=mode, window=window, start_at=start_at, hours=hours,
        expected_model_id=model_version_id,
        serving_runtime=getattr(request.app.state, "prediction_runtime", None),
    )

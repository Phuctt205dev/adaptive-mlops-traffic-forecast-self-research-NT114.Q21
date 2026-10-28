"""Admin-only reads. Viewing this screen never executes training or SHAP."""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.api.dependencies import require_admin
from backend.app.core.config import get_settings
from backend.app.db.session import get_db
from backend.app.services import training_execution


router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/training-monitor/capabilities")
def capabilities():
    settings = get_settings()
    return {"execution_control": settings.training_control_enabled,
            "cloud_training": settings.training_control_enabled and settings.cloud_training_enabled,
            "default_backend": "local", "automatic_fallback": False}


@router.get("/regions/{region_id}/training-monitor")
def monitor(region_id: uuid.UUID, db: Session = Depends(get_db)):
    if not get_settings().training_control_enabled:
        return {"items": [], "enabled": False}
    return {"items": training_execution.visible_runs(db, region_id), "enabled": True}


@router.post("/training-monitor/{run_id}/dismiss")
def dismiss(run_id: uuid.UUID, db: Session = Depends(get_db)):
    training_execution.dismiss_error(db, run_id)
    return {"dismissed": True}

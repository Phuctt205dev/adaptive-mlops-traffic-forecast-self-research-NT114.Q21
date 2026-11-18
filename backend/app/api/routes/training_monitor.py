"""Admin-only reads. Viewing this screen never executes training or SHAP."""
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from typing import Literal
from sqlalchemy.orm import Session

from backend.app.api.dependencies import require_admin
from backend.app.core.config import get_settings
from backend.app.db.session import get_db
from backend.app.services import training_execution


router = APIRouter(dependencies=[Depends(require_admin)])


class RetryRequest(BaseModel):
    training_backend: Literal["kaggle", "local"]
    request_id: uuid.UUID | None = None


@router.get("/training-monitor/capabilities")
def capabilities():
    settings = get_settings()
    from backend.app.services.training_policy import local_ready
    local = local_ready(settings)
    if local:
        import requests
        with requests.Session() as client:
            client.trust_env = False
            for service in ("traffic-training-tree", "traffic-training-worker"):
                response = None
                try:
                    response = client.get(f"http://{service}:8000/health/local-training", timeout=(1, 2), allow_redirects=False)
                    response.raise_for_status()
                    health = response.json()
                    local = local and health.get("local_training") is True and health.get("image") == settings.local_training_image
                except Exception:
                    local = False
                finally:
                    if response is not None:
                        response.close()
    return {"execution_control": settings.training_control_enabled,
            "cloud_training": settings.training_control_enabled and settings.cloud_training_enabled,
            "default_backend": "kaggle", "supported_backends": ["kaggle", *(["local"] if local else [])],
            "local_training": local, "automatic_fallback": False,
            "local_unavailable_reason": None if local else "Local training has not been configured and verified on RKE2."}


@router.post("/training-monitor/{run_id}/cancel")
def cancel(run_id: uuid.UUID, db: Session = Depends(get_db)):
    record = training_execution.request_cancel(db, run_id)
    return {"training_run_id": str(run_id), "state": record.state,
            "stop_requested": record.cancel_requested, "stop_confirmed": record.state == "cancelled"}


@router.post("/training-monitor/{run_id}/retry", status_code=202)
def retry(run_id: uuid.UUID, payload: RetryRequest, request: Request,
          db: Session = Depends(get_db), admin=Depends(require_admin)):
    from backend.app.db.models import TrainingRun
    from backend.app.schemas.model_version import TrainingRunCreate
    from backend.app.core.errors import ApplicationError
    from backend.app.api.routes.datasets import trigger_dataset_training
    parent = db.get(TrainingRun, run_id)
    if parent is None:
        raise ApplicationError("training_run_not_found", "Training run was not found.", 404)
    configuration = {**parent.configuration_json, "training_backend": payload.training_backend, "retry_of": str(run_id),
                     "request_id": str(payload.request_id or uuid.uuid4())}
    return trigger_dataset_training(parent.dataset_id, TrainingRunCreate.model_validate(configuration),
                                    db=db, admin=admin, request=request)


@router.get("/regions/{region_id}/training-monitor")
def monitor(region_id: uuid.UUID, db: Session = Depends(get_db)):
    if not get_settings().training_control_enabled:
        return {"items": [], "enabled": False}
    return {"items": training_execution.visible_runs(db, region_id), "enabled": True}


@router.post("/training-monitor/{run_id}/dismiss")
def dismiss(run_id: uuid.UUID, db: Session = Depends(get_db)):
    training_execution.dismiss_error(db, run_id)
    return {"dismissed": True}

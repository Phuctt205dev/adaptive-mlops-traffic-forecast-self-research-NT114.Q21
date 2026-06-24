import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from backend.app.api.dependencies import require_admin
from backend.app.db.session import get_db
from backend.app.schemas.model_version import (
    ModelActivationResponse,
    ModelVersionList,
    ModelVersionRead,
    TrainingRunRead,
)
from backend.app.services import model_registry


router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/regions/{region_id}/model-versions", response_model=ModelVersionList)
def list_region_model_versions(
    region_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    items, total = model_registry.list_region_model_versions(
        db,
        region_id,
        page,
        page_size,
    )
    return ModelVersionList(items=items, page=page, page_size=page_size, total=total)


@router.get("/model-versions/{model_version_id}", response_model=ModelVersionRead)
def get_model_version(
    model_version_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    return model_registry.get_model_version_read(db, model_version_id)


@router.get("/training-runs/{training_run_id}", response_model=TrainingRunRead)
def get_training_run(
    training_run_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    return model_registry.get_training_run_or_404(db, training_run_id)


@router.post(
    "/model-versions/{model_version_id}/activate",
    response_model=ModelActivationResponse,
)
def activate_model_version(
    model_version_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    model_version = model_registry.activate_model_version(db, model_version_id)
    return ModelActivationResponse(active_model_version=model_version)


@router.delete("/model-versions/{model_version_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model_version(
    model_version_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    model_registry.delete_model_version(db, model_version_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

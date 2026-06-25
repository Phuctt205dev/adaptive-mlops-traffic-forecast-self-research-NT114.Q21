import uuid

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy.orm import Session

from backend.app.api.dependencies import require_admin
from backend.app.core.config import get_settings
from backend.app.db.models import User
from backend.app.db.session import get_db
from backend.app.schemas.dataset import DatasetList, DatasetRead, DatasetUploadResponse
from backend.app.schemas.model_version import (
    TrainingRunCreate,
    TrainingRunTriggerResponse,
)
from backend.app.services import airflow_client, model_registry
from backend.app.services import datasets as dataset_service


router = APIRouter(dependencies=[Depends(require_admin)])


@router.post(
    "/regions/{region_id}/datasets",
    response_model=DatasetUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_region_dataset(
    region_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    content = await file.read()
    dataset, report = dataset_service.upload_region_dataset(
        db,
        region_id,
        admin,
        file.filename or "",
        content,
        file.content_type,
    )
    return DatasetUploadResponse(dataset=dataset, quality_report=report)


@router.get("/regions/{region_id}/datasets", response_model=DatasetList)
def list_region_datasets(
    region_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    items, total = dataset_service.list_region_datasets(
        db,
        region_id,
        page,
        page_size,
    )
    return DatasetList(items=items, page=page, page_size=page_size, total=total)


@router.get("/datasets/{dataset_id}", response_model=DatasetRead)
def get_dataset(dataset_id: uuid.UUID, db: Session = Depends(get_db)):
    return dataset_service.get_dataset_or_404(db, dataset_id)


@router.post(
    "/datasets/{dataset_id}/training-runs",
    response_model=TrainingRunTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_dataset_training(
    dataset_id: uuid.UUID,
    payload: TrainingRunCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    settings = get_settings()
    dataset = dataset_service.get_dataset_or_404(db, dataset_id)
    dag_run_id = f"training-{dataset_id}-{uuid.uuid4()}"
    configuration = payload.model_dump()
    training_run = model_registry.create_queued_training_run(
        db,
        dataset_id,
        admin.id,
        configuration,
        dag_run_id,
    )
    conf = {
        **configuration,
        "region_id": str(dataset.region_id),
        "dataset_id": str(dataset.id),
        "training_run_id": str(training_run.id),
    }
    airflow_response = airflow_client.trigger_dag(
        settings.training_dag_id,
        conf,
        dag_run_id=dag_run_id,
    )
    return TrainingRunTriggerResponse(
        training_run=training_run,
        dag_id=settings.training_dag_id,
        dag_run_id=dag_run_id,
        airflow_response=airflow_response,
    )

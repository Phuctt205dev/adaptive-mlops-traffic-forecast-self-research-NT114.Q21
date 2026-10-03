import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from backend.app.db.models import DatasetStatus


class DatasetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    region_id: uuid.UUID
    contract_version: str
    original_filename: str
    storage_uri: str
    sha256: str
    dvc_rev: str | None
    row_count: int | None
    start_at: datetime | None
    end_at: datetime | None
    status: DatasetStatus
    quality_report_uri: str | None
    validation_error: str | None
    uploaded_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class DatasetList(BaseModel):
    items: list[DatasetRead]
    page: int
    page_size: int
    total: int


class DatasetUploadResponse(BaseModel):
    dataset: DatasetRead
    quality_report: dict

import hashlib
import json
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.errors import ApplicationError
from backend.app.db.models import Dataset, DatasetStatus, Region, User
from backend.app.services import storage


REQUIRED_COLUMNS = [
    "date_time",
    "is_holiday",
    "air_pollution_index",
    "humidity",
    "wind_speed",
    "wind_direction",
    "visibility_in_miles",
    "dew_point",
    "temperature",
    "rain_p_h",
    "snow_p_h",
    "clouds_all",
    "weather_type",
    "weather_description",
    "traffic_volume",
]

NUMERIC_RULES = {
    "air_pollution_index": {"min": 0},
    "humidity": {"min": 0, "max": 100},
    "wind_speed": {"min": 0},
    "wind_direction": {"min": 0, "max": 360},
    "visibility_in_miles": {"min": 0},
    "dew_point": {},
    "temperature": {},
    "rain_p_h": {"min": 0},
    "snow_p_h": {"min": 0},
    "clouds_all": {"min": 0, "max": 100},
    "traffic_volume": {"min": 0},
}


@dataclass(frozen=True)
class DatasetValidationResult:
    row_count: int
    start_at: object
    end_at: object
    report: dict


def list_region_datasets(
    db: Session,
    region_id: uuid.UUID,
    page: int,
    page_size: int,
) -> tuple[list[Dataset], int]:
    ensure_region_exists(db, region_id)
    query = select(Dataset).where(Dataset.region_id == region_id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    datasets = list(
        db.scalars(
            query.order_by(Dataset.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return datasets, total


def get_dataset_or_404(db: Session, dataset_id: uuid.UUID) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise ApplicationError("dataset_not_found", "Dataset was not found.", 404)
    return dataset


def upload_region_dataset(
    db: Session,
    region_id: uuid.UUID,
    uploader: User,
    filename: str,
    content: bytes,
    content_type: str | None,
) -> tuple[Dataset, dict]:
    settings = get_settings()
    ensure_region_exists(db, region_id)
    validate_upload_file(filename, content)

    sha256 = hashlib.sha256(content).hexdigest()
    duplicate = db.scalar(
        select(Dataset).where(
            Dataset.region_id == region_id,
            Dataset.sha256 == sha256,
        )
    )
    if duplicate is not None:
        raise ApplicationError(
            "dataset_duplicate",
            "This dataset file was already uploaded for the region.",
            409,
        )

    object_prefix = f"regions/{region_id}/datasets/{sha256}"
    raw_key = f"{object_prefix}/{Path(filename).name}"
    report_key = f"{object_prefix}/quality-report.json"
    raw_uri = storage.put_object(
        settings.dataset_bucket,
        raw_key,
        content,
        content_type or "text/csv",
    )

    try:
        validation = validate_training_csv(content)
        report = validation.report
        report_uri = storage.put_object(
            settings.dataset_bucket,
            report_key,
            json.dumps(report, indent=2, default=str).encode("utf-8"),
            "application/json",
        )
        dataset = Dataset(
            region_id=region_id,
            contract_version=settings.dataset_contract_version,
            original_filename=Path(filename).name,
            storage_uri=raw_uri,
            sha256=sha256,
            row_count=validation.row_count,
            start_at=validation.start_at,
            end_at=validation.end_at,
            status=DatasetStatus.VALID,
            quality_report_uri=report_uri,
            uploaded_by=uploader.id,
        )
    except ApplicationError as error:
        report = {
            "contract_version": settings.dataset_contract_version,
            "status": "validation_failed",
            "error": {
                "code": error.code,
                "message": error.message,
            },
        }
        report_uri = storage.put_object(
            settings.dataset_bucket,
            report_key,
            json.dumps(report, indent=2).encode("utf-8"),
            "application/json",
        )
        dataset = Dataset(
            region_id=region_id,
            contract_version=settings.dataset_contract_version,
            original_filename=Path(filename).name,
            storage_uri=raw_uri,
            sha256=sha256,
            status=DatasetStatus.VALIDATION_FAILED,
            quality_report_uri=report_uri,
            validation_error=error.message,
            uploaded_by=uploader.id,
        )

    db.add(dataset)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise ApplicationError(
            "dataset_duplicate",
            "This dataset file was already uploaded for the region.",
            409,
        ) from error
    db.refresh(dataset)
    return dataset, report


def ensure_region_exists(db: Session, region_id: uuid.UUID) -> Region:
    region = db.get(Region, region_id)
    if region is None:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    return region


def validate_upload_file(filename: str, content: bytes) -> None:
    settings = get_settings()
    if not filename or Path(filename).suffix.lower() != ".csv":
        raise ApplicationError(
            "invalid_dataset_file",
            "Dataset upload must be a CSV file.",
            400,
        )
    if not content:
        raise ApplicationError("empty_dataset_file", "Dataset file is empty.", 400)
    if len(content) > settings.dataset_max_upload_bytes:
        raise ApplicationError(
            "dataset_too_large",
            "Dataset file exceeds the configured upload limit.",
            413,
        )


def validate_training_csv(content: bytes) -> DatasetValidationResult:
    settings = get_settings()
    try:
        frame = pd.read_csv(BytesIO(content))
    except Exception as error:
        raise ApplicationError(
            "dataset_csv_invalid",
            "Dataset file could not be parsed as CSV.",
            400,
        ) from error

    missing_columns = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing_columns:
        raise ApplicationError(
            "dataset_schema_invalid",
            f"Dataset is missing required columns: {', '.join(missing_columns)}.",
            400,
        )

    row_count = len(frame)
    if row_count < settings.dataset_min_rows:
        raise ApplicationError(
            "dataset_too_short",
            f"Dataset must contain at least {settings.dataset_min_rows} rows.",
            400,
        )

    timestamps = pd.to_datetime(frame["date_time"], errors="coerce")
    if timestamps.isna().any():
        raise ApplicationError(
            "dataset_timestamp_invalid",
            "Dataset contains invalid date_time values.",
            400,
        )
    if timestamps.duplicated().any():
        raise ApplicationError(
            "dataset_timestamp_duplicate",
            "Dataset contains duplicate date_time values.",
            400,
        )

    for column, rules in NUMERIC_RULES.items():
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any():
            raise ApplicationError(
                "dataset_numeric_invalid",
                f"Column {column} contains invalid numeric values.",
                400,
            )
        if "min" in rules and (values < rules["min"]).any():
            raise ApplicationError(
                "dataset_range_invalid",
                f"Column {column} contains values below {rules['min']}.",
                400,
            )
        if "max" in rules and (values > rules["max"]).any():
            raise ApplicationError(
                "dataset_range_invalid",
                f"Column {column} contains values above {rules['max']}.",
                400,
            )
        if "lt" in rules and (values >= rules["lt"]).any():
            raise ApplicationError(
                "dataset_range_invalid",
                f"Column {column} contains values greater than or equal to {rules['lt']}.",
                400,
            )

    sorted_timestamps = timestamps.sort_values()
    report = {
        "contract_version": settings.dataset_contract_version,
        "rows_raw": row_count,
        "start": sorted_timestamps.iloc[0].isoformat(),
        "end": sorted_timestamps.iloc[-1].isoformat(),
        "duplicate_timestamps": int(timestamps.duplicated().sum()),
        "missing_required_columns": [],
        "warnings": [],
    }
    return DatasetValidationResult(
        row_count=row_count,
        start_at=sorted_timestamps.iloc[0].to_pydatetime(),
        end_at=sorted_timestamps.iloc[-1].to_pydatetime(),
        report=report,
    )

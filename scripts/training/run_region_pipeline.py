import argparse
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import boto3
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.db.models import Dataset, DatasetStatus, TrainingRun
from backend.app.db.models import TrainingRunStatus
from backend.app.services.model_registry import register_training_result
from src.pipeline import run_pipeline


def parse_args():
    parser = argparse.ArgumentParser(
        description="Train a traffic model for one uploaded region dataset."
    )
    parser.add_argument("--region-id", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--train-start-date", required=True)
    parser.add_argument("--train-end-date", required=True)
    parser.add_argument("--artifact-root", default="models/regions")
    parser.add_argument("--model-role", default="candidate")
    parser.add_argument("--cv-splits", type=int, default=3)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--local-data-path")
    parser.add_argument("--training-run-id")
    return parser.parse_args()


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_ENDPOINT_URL", "http://localhost:9000"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin123"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def download_dataset(storage_uri: str, destination: Path) -> Path:
    bucket, key = parse_s3_uri(storage_uri)
    destination.parent.mkdir(parents=True, exist_ok=True)
    get_s3_client().download_file(bucket, key, str(destination))
    return destination


def load_dataset_metadata(dataset_id: uuid.UUID, region_id: uuid.UUID) -> Dataset:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required.")

    engine = create_engine(database_url, pool_pre_ping=True)
    with Session(engine) as session:
        dataset = session.scalar(
            select(Dataset).where(
                Dataset.id == dataset_id,
                Dataset.region_id == region_id,
            )
        )
        if dataset is None:
            raise RuntimeError("Dataset was not found for the selected region.")
        if dataset.status != DatasetStatus.VALID:
            raise RuntimeError(
                f"Dataset must be valid before training. Current status: {dataset.status.value}"
            )
        session.expunge(dataset)
        return dataset


def register_model_version(
    region_id: uuid.UUID,
    dataset_id: uuid.UUID,
    model_info: dict,
    training_run_id: uuid.UUID | None = None,
) -> dict:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required.")

    engine = create_engine(database_url, pool_pre_ping=True)
    with Session(engine) as session:
        training_run, model_version = register_training_result(
            session,
            region_id,
            dataset_id,
            model_info,
            training_run_id=training_run_id,
        )
        return {
            "training_run_id": str(training_run.id),
            "model_version_id": str(model_version.id),
            "model_version_status": model_version.status.value,
        }


def main() -> int:
    args = parse_args()
    region_id = uuid.UUID(args.region_id)
    dataset_id = uuid.UUID(args.dataset_id)
    training_run_id = (
        uuid.UUID(args.training_run_id)
        if args.training_run_id
        else None
    )
    dataset = load_dataset_metadata(dataset_id, region_id)

    if args.local_data_path:
        data_path = Path(args.local_data_path)
    else:
        data_path = Path("data/region_uploads") / str(region_id) / f"{dataset_id}.csv"
        download_dataset(dataset.storage_uri, data_path)

    if training_run_id:
        database_url = os.getenv("DATABASE_URL")
        engine = create_engine(database_url, pool_pre_ping=True)
        with Session(engine) as session:
            training_run = session.get(TrainingRun, training_run_id)
            if training_run is not None:
                training_run.status = TrainingRunStatus.TRAINING
                session.commit()

    model_dir = Path(args.artifact_root) / str(region_id) / "models"
    output_model_path = model_dir / f"{args.model_role}_model.pkl"
    output_info_path = model_dir / f"{args.model_role}_model_info.json"

    model_info = run_pipeline(
        train_start_date=args.train_start_date,
        train_end_date=args.train_end_date,
        output_model_path=str(output_model_path),
        output_info_path=str(output_info_path),
        model_role=args.model_role,
        random_state=args.random_state,
        cv_splits=args.cv_splits,
        data_path=str(data_path),
        region_id=region_id,
        dataset_id=dataset_id,
        artifact_root=args.artifact_root,
    )
    registry_info = register_model_version(
        region_id,
        dataset_id,
        model_info,
        training_run_id=training_run_id,
    )
    model_info.update(registry_info)
    print(model_info)
    return 0


if __name__ == "__main__":
    sys.exit(main())

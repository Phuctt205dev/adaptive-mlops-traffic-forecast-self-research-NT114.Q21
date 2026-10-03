import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.model_registry import register_training_result


def parse_args():
    parser = argparse.ArgumentParser(
        description="Register an existing model info JSON into the DB registry."
    )
    parser.add_argument("--region-id", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--model-info-path", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required.")

    with open(args.model_info_path, "r", encoding="utf-8") as file:
        model_info = json.load(file)

    region_id = uuid.UUID(args.region_id)
    dataset_id = uuid.UUID(args.dataset_id)
    engine = create_engine(database_url, pool_pre_ping=True)
    with Session(engine) as session:
        training_run, model_version = register_training_result(
            session,
            region_id,
            dataset_id,
            model_info,
        )
        print(
            {
                "training_run_id": str(training_run.id),
                "model_version_id": str(model_version.id),
                "model_version_status": model_version.status.value,
            }
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

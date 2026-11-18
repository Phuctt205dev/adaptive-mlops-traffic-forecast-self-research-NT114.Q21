"""Recover a lost admin HTTP response without dispatching the same work twice."""
import hashlib
from sqlalchemy import select, text
from backend.app.core.errors import ApplicationError
from backend.app.db.models import TrainingRun
from backend.app.schemas.model_version import TrainingRunCreate


def existing_submission(db, dataset_id, admin_id, configuration):
    request_id = configuration.get("request_id")
    if not request_id:
        return None  # Legacy callers retain their existing API contract.
    key = int.from_bytes(hashlib.sha256((str(admin_id) + ":" + str(request_id)).encode()).digest()[:8], "big", signed=True)
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    existing = db.scalar(select(TrainingRun).where(TrainingRun.requested_by == admin_id,
        TrainingRun.configuration_json["request_id"].as_string() == str(request_id)))
    if existing:
        original = TrainingRunCreate.model_validate(existing.configuration_json).model_dump(mode="json")
        incoming = TrainingRunCreate.model_validate(configuration).model_dump(mode="json")
        if existing.dataset_id != dataset_id or original != incoming:
            raise ApplicationError("training_submission_changed", "This submission already exists with different settings. Check Training Monitor before starting another run.", 409)
    # The caller creates/commits under this same transaction when no row exists.
    return existing

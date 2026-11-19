"""Recover a lost admin HTTP response without dispatching the same work twice."""
import hashlib
from datetime import datetime, time
from pydantic import ValidationError
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
        stored = existing.configuration_json
        if isinstance(stored, dict):
            stored = dict(stored)
            # Neural publication records midnight datetime strings. Compare
            # their original calendar window without rewriting history or
            # allowing datetime input on new browser submissions.
            for field in ('train_start_date', 'train_end_date'):
                value = stored.get(field)
                if isinstance(value, str):
                    try:
                        parsed = datetime.fromisoformat(value)
                    except ValueError:
                        continue
                    if parsed.tzinfo is None and parsed.time() == time():
                        stored[field] = parsed.date().isoformat()
        try:
            original = TrainingRunCreate.model_validate(stored).model_dump(mode="json")
        except ValidationError:
            raise ApplicationError("training_submission_changed",
                "The saved submission cannot be compared safely. Check Training Monitor before starting another run.", 409) from None
        incoming = TrainingRunCreate.model_validate(configuration).model_dump(mode="json")
        if existing.dataset_id != dataset_id or original != incoming:
            raise ApplicationError("training_submission_changed", "This submission already exists with different settings. Check Training Monitor before starting another run.", 409)
    # The caller creates/commits under this same transaction when no row exists.
    return existing

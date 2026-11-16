"""Durable region preferences in the existing audited configuration store.

No process-local cache: both API replicas and scheduled checks read the same DB.
An absent preference is an opt-out, including regions created before this feature.
"""
from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.db.models import AuditLog, Region


ACTION = "region_drift_settings_updated"


def _latest(db: Session, region_id: uuid.UUID) -> AuditLog | None:
    return db.scalar(
        select(AuditLog)
        .where(AuditLog.entity_type == "region", AuditLog.entity_id == region_id, AuditLog.action == ACTION)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    )


def auto_retrain_enabled(db: Session, region_id: uuid.UUID) -> bool:
    entry = _latest(db, region_id)
    return entry is not None and (entry.after_json or {}).get("auto_retrain") is True


def get_settings(db: Session, region_id: uuid.UUID) -> dict:
    if db.get(Region, region_id) is None:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    return {"region_id": str(region_id), "auto_retrain": auto_retrain_enabled(db, region_id)}


def update_settings(db: Session, region_id: uuid.UUID, enabled: bool, actor_id: uuid.UUID) -> dict:
    # Serialize writes for this region across replicas; keep the preference changes
    # as audit events rather than requiring a runtime/schema migration.
    region = db.scalar(select(Region).where(Region.id == region_id).with_for_update())
    if region is None:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    previous = _latest(db, region_id)
    before = previous is not None and (previous.after_json or {}).get("auto_retrain") is True
    if before != enabled:
        changed_at = datetime.now(UTC)
        if previous is not None:
            last_at = previous.created_at
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=UTC)
            # Preserve change ordering even if replica clocks differ slightly.
            changed_at = max(changed_at, last_at + timedelta(microseconds=1))
        db.add(AuditLog(actor_user_id=actor_id, action=ACTION, entity_type="region", entity_id=region_id,
            before_json={"auto_retrain": before}, after_json={"auto_retrain": enabled}, created_at=changed_at))
    db.commit()
    return {"region_id": str(region_id), "auto_retrain": enabled}

"""Execution state is separate from immutable ML configuration/receipt hashes."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.models import Base


class TrainingExecution(Base):
    __tablename__ = "training_executions"

    training_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("training_runs.id", ondelete="CASCADE"), primary_key=True)
    backend: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(500))
    branches_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    models_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    events_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

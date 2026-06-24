"""Add drift checks table.

Revision ID: 20260624_01
Revises: 20260620_01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260624_01"
down_revision: Union[str, None] = "20260620_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


drift_check_status = sa.Enum(
    "stable",
    "drift_detected",
    "retrain_triggered",
    "skipped",
    "failed",
    name="drift_check_status",
)


def upgrade() -> None:
    drift_check_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "drift_checks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("region_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=True),
        sa.Column("active_model_version_id", sa.Uuid(), nullable=True),
        sa.Column("reference_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reference_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", drift_check_status, nullable=False),
        sa.Column("drift_detected", sa.Boolean(), nullable=False),
        sa.Column("drifted_feature_count", sa.Integer(), nullable=False),
        sa.Column("feature_count", sa.Integer(), nullable=False),
        sa.Column("feature_drift_json", sa.JSON(), nullable=False),
        sa.Column("triggered_training_run_id", sa.Uuid(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["active_model_version_id"], ["model_versions.id"]),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"]),
        sa.ForeignKeyConstraint(["region_id"], ["regions.id"]),
        sa.ForeignKeyConstraint(["triggered_training_run_id"], ["training_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_drift_checks_region_created_at",
        "drift_checks",
        ["region_id", "created_at"],
    )
    op.create_index("ix_drift_checks_status", "drift_checks", ["status"])


def downgrade() -> None:
    op.drop_index("ix_drift_checks_status", table_name="drift_checks")
    op.drop_index("ix_drift_checks_region_created_at", table_name="drift_checks")
    op.drop_table("drift_checks")
    drift_check_status.drop(op.get_bind(), checkfirst=True)

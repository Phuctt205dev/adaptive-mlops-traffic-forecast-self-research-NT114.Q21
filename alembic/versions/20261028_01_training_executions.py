"""Separate training execution/progress from immutable ML configuration."""
from alembic import op
import sqlalchemy as sa

revision = "20261028_01"
down_revision = "20260625_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "training_executions",
        sa.Column("training_run_id", sa.Uuid(), sa.ForeignKey("training_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("backend", sa.String(16), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("dismissed", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(80)), sa.Column("error_message", sa.String(500)),
        sa.Column("branches_json", sa.JSON(), nullable=False), sa.Column("models_json", sa.JSON(), nullable=False),
        sa.Column("events_json", sa.JSON(), nullable=False), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_training_executions_state", "training_executions", ["state"])


def downgrade():
    op.drop_index("ix_training_executions_state", table_name="training_executions")
    op.drop_table("training_executions")

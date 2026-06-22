"""add user display name

Revision ID: 20260620_01
Revises: 20260611_01
Create Date: 2026-06-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260620_01"
down_revision = "20260611_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("users")}
    if "display_name" not in columns:
        op.add_column("users", sa.Column("display_name", sa.String(length=160), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("users")}
    if "display_name" in columns:
        op.drop_column("users", "display_name")

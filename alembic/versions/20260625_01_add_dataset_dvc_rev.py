"""Add DVC revision to datasets.

Revision ID: 20260625_01
Revises: 20260624_01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "20260625_01"
down_revision: Union[str, None] = "20260624_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("datasets")}
    if "dvc_rev" not in columns:
        op.add_column("datasets", sa.Column("dvc_rev", sa.String(length=255), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("datasets")}
    if "dvc_rev" in columns:
        op.drop_column("datasets", "dvc_rev")

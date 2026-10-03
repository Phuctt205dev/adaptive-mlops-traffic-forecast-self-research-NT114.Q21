"""Initial management schema.

Revision ID: 20260611_01
Revises:
"""
from typing import Sequence, Union

from alembic import op

from backend.app.db.base import Base


revision: str = "20260611_01"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())

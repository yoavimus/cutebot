"""instagram external ids

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("ig_container_id", sa.String(64), nullable=True))
    op.add_column("posts", sa.Column("ig_media_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "ig_media_id")
    op.drop_column("posts", "ig_container_id")

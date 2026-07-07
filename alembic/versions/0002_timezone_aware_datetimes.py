"""timezone-aware datetimes

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-07 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)
_USING = "AT TIME ZONE 'UTC'"


def upgrade() -> None:
    op.alter_column("batches", "created_at", type_=_TZ, postgresql_using=f"created_at {_USING}")
    op.alter_column("posts", "created_at", type_=_TZ, postgresql_using=f"created_at {_USING}")
    op.alter_column("posts", "decided_at", type_=_TZ, postgresql_using=f"decided_at {_USING}")
    op.alter_column("posts", "published_at", type_=_TZ, postgresql_using=f"published_at {_USING}")
    op.alter_column("feedback", "created_at", type_=_TZ, postgresql_using=f"created_at {_USING}")


def downgrade() -> None:
    _NAIVE = sa.DateTime(timezone=False)
    op.alter_column("batches", "created_at", type_=_NAIVE)
    op.alter_column("posts", "created_at", type_=_NAIVE)
    op.alter_column("posts", "decided_at", type_=_NAIVE)
    op.alter_column("posts", "published_at", type_=_NAIVE)
    op.alter_column("feedback", "created_at", type_=_NAIVE)

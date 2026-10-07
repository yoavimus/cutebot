"""feedback edit columns (✏️ Fix)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("feedback", sa.Column("edit_lang", sa.String(2), nullable=True))
    op.add_column("feedback", sa.Column("edit_before", sa.Text(), nullable=True))
    op.add_column("feedback", sa.Column("edit_after", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("feedback", "edit_after")
    op.drop_column("feedback", "edit_before")
    op.drop_column("feedback", "edit_lang")

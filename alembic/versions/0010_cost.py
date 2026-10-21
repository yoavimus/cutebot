"""cost per post / batch (M10.3)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("tokens_in", sa.Integer(), nullable=True))
    op.add_column("posts", sa.Column("tokens_out", sa.Integer(), nullable=True))
    op.add_column("posts", sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True))
    op.add_column("batches", sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True))


def downgrade() -> None:
    op.drop_column("batches", "cost_usd")
    op.drop_column("posts", "cost_usd")
    op.drop_column("posts", "tokens_out")
    op.drop_column("posts", "tokens_in")

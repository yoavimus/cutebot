"""posts.retry_of (M9 verdicts: Keep / Almost / Retry)

Statuses and decisions are String(16) columns, so ``kept`` / ``almost`` need no DDL.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-15 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "posts",
        sa.Column("retry_of", sa.Integer(), sa.ForeignKey("posts.id"), nullable=True),
    )
    op.create_index("ix_posts_retry_of", "posts", ["retry_of"])


def downgrade() -> None:
    op.drop_index("ix_posts_retry_of", table_name="posts")
    op.drop_column("posts", "retry_of")

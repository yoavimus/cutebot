"""stock_images provenance table + brand Drive columns (M11.1)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_images",
        sa.Column("brand_id", sa.Integer(), sa.ForeignKey("brands.id"), primary_key=True),
        sa.Column("image_ref", sa.Text(), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(256), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_stock_images_brand_sha256", "stock_images", ["brand_id", "sha256"])
    op.add_column("brands", sa.Column("drive_folder_id", sa.String(128), nullable=True))
    op.add_column("brands", sa.Column("drive_synced_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("brands", "drive_synced_at")
    op.drop_column("brands", "drive_folder_id")
    op.drop_index("ix_stock_images_brand_sha256", table_name="stock_images")
    op.drop_table("stock_images")

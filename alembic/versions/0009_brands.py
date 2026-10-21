"""brands table + brand_id on every row (M10.1 tenant seam)

Postgres-only like 0002+. Row 1 ("default") gets its chat id from ``TELEGRAM_CHAT_ID``
and empty text; the app seeds the text from ``BRAND_FILE`` at boot (``brands.ensure_default``)
because the volume file is the app's to read, not Alembic's. New columns go nullable →
backfill 1 → NOT NULL. ``settings`` and ``banned_images`` get composite primary keys.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-21 00:00:00.000000

"""

import os
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXED = ("posts", "batches", "feedback")


def upgrade() -> None:
    op.create_table(
        "brands",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("telegram_chat_id", sa.String(64), nullable=True, unique=True),
        sa.Column("brand_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("brand_text_prev", sa.Text(), nullable=True),
        sa.Column("stock_prefix", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None
    op.execute(
        sa.text(
            "INSERT INTO brands (id, name, telegram_chat_id) VALUES (1, 'default', :chat)"
        ).bindparams(chat=chat_id)
    )
    # explicit id=1 does not advance the serial; the next brand must not collide with it
    op.execute("SELECT setval(pg_get_serial_sequence('brands', 'id'), 1)")

    for table in (*_INDEXED, "settings"):
        op.add_column(table, sa.Column("brand_id", sa.Integer(), nullable=True))
        op.execute(f"UPDATE {table} SET brand_id = 1")
        op.alter_column(table, "brand_id", nullable=False)
        op.create_foreign_key(f"fk_{table}_brand_id", table, "brands", ["brand_id"], ["id"])
    for table in _INDEXED:
        op.create_index(f"ix_{table}_brand_id", table, ["brand_id"])

    # settings: PK key → (brand_id, key)
    op.drop_constraint("settings_pkey", "settings", type_="primary")
    op.create_primary_key("settings_pkey", "settings", ["brand_id", "key"])

    # banned_images: tiny table — recreate with the (brand_id, image_ref) key, copy rows over
    op.rename_table("banned_images", "banned_images_old")
    op.execute(
        "ALTER TABLE banned_images_old RENAME CONSTRAINT banned_images_pkey "
        "TO banned_images_old_pkey"
    )
    op.create_table(
        "banned_images",
        sa.Column("brand_id", sa.Integer(), sa.ForeignKey("brands.id"), primary_key=True),
        sa.Column("image_ref", sa.Text(), primary_key=True),
        sa.Column("banned_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "INSERT INTO banned_images (brand_id, image_ref, banned_at) "
        "SELECT 1, image_ref, banned_at FROM banned_images_old"
    )
    op.drop_table("banned_images_old")


def downgrade() -> None:
    # Multi-brand data cannot fold back into one tenant: keep brand 1 only.
    op.rename_table("banned_images", "banned_images_new")
    op.execute(
        "ALTER TABLE banned_images_new RENAME CONSTRAINT banned_images_pkey "
        "TO banned_images_new_pkey"
    )
    op.create_table(
        "banned_images",
        sa.Column("image_ref", sa.Text(), primary_key=True),
        sa.Column("banned_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "INSERT INTO banned_images (image_ref, banned_at) "
        "SELECT image_ref, banned_at FROM banned_images_new WHERE brand_id = 1"
    )
    op.drop_table("banned_images_new")

    op.execute("DELETE FROM settings WHERE brand_id <> 1")
    op.drop_constraint("settings_pkey", "settings", type_="primary")
    op.create_primary_key("settings_pkey", "settings", ["key"])

    for table in _INDEXED:
        op.drop_index(f"ix_{table}_brand_id", table_name=table)
    for table in (*_INDEXED, "settings"):
        op.drop_constraint(f"fk_{table}_brand_id", table, type_="foreignkey")
        op.drop_column(table, "brand_id")
    op.drop_table("brands")

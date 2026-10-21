"""ORM models — Brand, Post, Feedback, Batch. See PRODUCT_SPEC §4."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PostStatus(StrEnum):
    """Post lifecycle. See the state machine in PRODUCT_SPEC §2."""

    SUGGESTED = "suggested"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    # 📚 Keep (M9): approved for learning only. Never queued, never published; promoting it
    # to APPROVED goes through handle_decision like any approval.
    KEPT = "kept"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    # 🤏 Almost (M9): liked the direction, flawed execution. The post ends REJECTED.
    ALMOST = "almost"


class Brand(Base):
    """A tenant (M10). Every post, batch, feedback, banned image and setting belongs to one.

    ``brand_text`` is the source of truth for the brand guidelines; ``brand.md`` only seeds
    it (see ``app/brands.py``). ``stock_prefix`` = sub-folder of the stock library.
    ``brand_id`` columns have no ORM default on purpose: a row written without its brand
    must fail loudly, not land on brand 1.
    """

    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), unique=True, default=None)
    brand_text: Mapped[str] = mapped_column(Text, default="")
    brand_text_prev: Mapped[str | None] = mapped_column(Text, default=None)
    stock_prefix: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Batch(Base):
    """Provenance for one generation run."""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    model: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column()
    brand_snapshot: Mapped[str] = mapped_column(Text, default="")
    # Sum of its posts' cost; None = no priced usage (stub run or unknown model id).
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), default=None)

    posts: Mapped[list[Post]] = relationship(back_populates="batch")


class Post(Base):
    """A single suggested/approved/published post."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    image_ref: Mapped[str] = mapped_column(Text)
    caption_he: Mapped[str] = mapped_column(Text)
    caption_en: Mapped[str] = mapped_column(Text)
    visual_concept: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="")
    # LiteLLM usage of the caption call (M10.3); None when the provider reported none.
    tokens_in: Mapped[int | None] = mapped_column(default=None)
    tokens_out: Mapped[int | None] = mapped_column(default=None)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), default=None)

    status: Mapped[PostStatus] = mapped_column(String(16), default=PostStatus.SUGGESTED)
    queue_position: Mapped[int | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Instagram Graph progress markers — crash-safe idempotency for recover_orphaned
    # (M7.3). Generic enough for a future real network; IG-specific names are fine
    # until a second real adapter exists (YAGNI on a shared external_ids table).
    ig_container_id: Mapped[str | None] = mapped_column(String(64), default=None)
    ig_media_id: Mapped[str | None] = mapped_column(String(64), default=None)

    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), default=None)
    batch: Mapped[Batch | None] = relationship(back_populates="posts")

    # 🔁 Retry (M9): the 🤏 Almost'd post this one re-captions (same image).
    retry_of: Mapped[int | None] = mapped_column(ForeignKey("posts.id"), default=None, index=True)

    feedback: Mapped[list[Feedback]] = relationship(back_populates="post")


class BannedImage(Base):
    """A stock image the owner never wants suggested again (Telegram "🚫 Ban image").

    The file stays on disk so ``/media`` keeps serving posts that already used it; the
    DB is the only ledger of what's eligible (see docs/issues findings 2026-10-05 §2/§9).
    """

    __tablename__ = "banned_images"

    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), primary_key=True)
    image_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    banned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Feedback(Base):
    """The training signal — one row per approve/reject decision."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))
    decision: Mapped[Decision] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(32), default=None)
    # ✏️ Fix (M8.2): an approve-with-edits records the delta. Decision stays ``approve``.
    # ponytail: two text columns beat a JSON blob — the learning query reads them directly.
    edit_lang: Mapped[str | None] = mapped_column(String(2), default=None)  # "he" | "en"
    edit_before: Mapped[str | None] = mapped_column(Text, default=None)
    edit_after: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    post: Mapped[Post] = relationship(back_populates="feedback")


class Setting(Base):
    """Runtime preference (M9): ``mode`` / ``generation_cron`` / ``posting_slots``.

    Key/value per brand; no row = the env default (see ``app/prefs.py``).
    """

    __tablename__ = "settings"

    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

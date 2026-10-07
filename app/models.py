"""ORM models — Post, Feedback, Batch. See PRODUCT_SPEC §4."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text
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


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class Batch(Base):
    """Provenance for one generation run."""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    model: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column()
    brand_snapshot: Mapped[str] = mapped_column(Text, default="")

    posts: Mapped[list[Post]] = relationship(back_populates="batch")


class Post(Base):
    """A single suggested/approved/published post."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    image_ref: Mapped[str] = mapped_column(Text)
    caption_he: Mapped[str] = mapped_column(Text)
    caption_en: Mapped[str] = mapped_column(Text)
    visual_concept: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="")

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

    feedback: Mapped[list[Feedback]] = relationship(back_populates="post")


class BannedImage(Base):
    """A stock image the owner never wants suggested again (Telegram "🚫 Ban image").

    The file stays on disk so ``/media`` keeps serving posts that already used it; the
    DB is the only ledger of what's eligible (see docs/issues findings 2026-10-05 §2/§9).
    """

    __tablename__ = "banned_images"

    image_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    banned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Feedback(Base):
    """The training signal — one row per approve/reject decision."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
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

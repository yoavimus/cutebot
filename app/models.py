"""ORM models — Post, Feedback, Batch. See PRODUCT_SPEC §4."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, String, Text
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
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    model: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column()
    brand_snapshot: Mapped[str] = mapped_column(Text, default="")

    posts: Mapped[list[Post]] = relationship(back_populates="batch")


class Post(Base):
    """A single suggested/approved/published post."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    caption: Mapped[str] = mapped_column(Text)
    visual_concept: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[PostStatus] = mapped_column(String(16), default=PostStatus.SUGGESTED)
    queue_position: Mapped[int | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    published_at: Mapped[datetime | None] = mapped_column(default=None)

    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), default=None)
    batch: Mapped[Batch | None] = relationship(back_populates="posts")

    feedback: Mapped[list[Feedback]] = relationship(back_populates="post")


class Feedback(Base):
    """The training signal — one row per approve/reject decision."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))
    decision: Mapped[Decision] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    post: Mapped[Post] = relationship(back_populates="feedback")

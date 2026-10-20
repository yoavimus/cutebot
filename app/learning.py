"""Learning context (M8.1) — prompt context derived from the DB at generation time.

Pure selection: SQL over ``posts`` + ``feedback``, no model call, no cache. ponytail: at
``batch_size=5`` a day this is a handful of queries per batch; cache when it measurably
matters. With nothing approved/rejected/published the context is empty and renders to "",
so a cold start generates exactly as before.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import Settings
from app.models import Decision, Feedback, Post, PostStatus


class Example(BaseModel):
    caption_he: str
    caption_en: str
    edited: bool = False


class Edit(BaseModel):
    before: str
    after: str
    lang: str


class NearMiss(BaseModel):
    """A 🤏 Almost'd caption and the chip the owner picked (None = skipped)."""

    caption_he: str
    reason: str | None = None


class LearningContext(BaseModel):
    examples: list[Example] = []
    recent: list[str] = []
    reject_hints: dict[str, int] = {}
    edits: list[Edit] = []
    near_misses: list[NearMiss] = []
    # Set only by review.retry(): the Almost'd caption this generation is retrying.
    retry_of: NearMiss | None = None

    def summary(self) -> str:
        """One line for /status and the eval header: what the model will see."""
        hints = sum(self.reject_hints.values())
        return (
            f"{len(self.examples)} examples · {len(self.edits)} edits · "
            f"{len(self.recent)} recent · {hints} reject hints · "
            f"{len(self.near_misses)} near-misses"
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.examples
            or self.recent
            or self.reject_hints
            or self.edits
            or self.near_misses
            or self.retry_of
        )


# `image` is deliberately absent: that's a selection problem, handled by ban_image in stock.
_HINTS = {
    "hebrew": "unnatural Hebrew — write as a native speaker would actually say it",
    "voice": "off-brand voice — re-read the guidelines",
    "boring": "being boring — take a sharper angle",
}
_REPEATEDLY = 3
_LIVE = (PostStatus.APPROVED, PostStatus.KEPT, PostStatus.PUBLISHING, PostStatus.PUBLISHED)


async def build_context(
    session: AsyncSession, brand_id: int, settings: Settings
) -> LearningContext:
    """The brand's own history only — every query filters on ``brand_id``."""
    if not settings.learning_enabled:
        return LearningContext()

    post_mine = Post.brand_id == brand_id
    fb_mine = Feedback.brand_id == brand_id
    edited_ids = select(Feedback.post_id).where(fb_mine, Feedback.edit_after.is_not(None))
    # Edited posts first (the owner's corrected voice), then newest first.
    rows = await session.scalars(
        select(Post)
        .where(post_mine, Post.status.in_(_LIVE))
        .order_by(Post.id.in_(edited_ids).desc(), Post.decided_at.desc(), Post.id.desc())
        .limit(settings.learning_examples)
    )
    posts = list(rows)
    edited = set(
        await session.scalars(
            select(Feedback.post_id).where(
                fb_mine,
                Feedback.edit_after.is_not(None),
                Feedback.post_id.in_([p.id for p in posts]),
            )
        )
    )
    examples = [
        Example(caption_he=p.caption_he, caption_en=p.caption_en, edited=p.id in edited)
        for p in posts
    ]

    edit_rows = await session.scalars(
        select(Feedback)
        .where(fb_mine, Feedback.edit_after.is_not(None))
        .order_by(Feedback.id.desc())
        .limit(settings.learning_examples)
    )
    edits = [
        Edit(before=f.edit_before or "", after=f.edit_after or "", lang=f.edit_lang or "he")
        for f in edit_rows
    ]
    # 🔁 Retry pairs: an Almost'd caption → its liked retry reads exactly like a ✏️ Fix.
    parent = aliased(Post)
    pairs = await session.execute(
        select(parent.caption_he, Post.caption_he)
        .join(parent, Post.retry_of == parent.id)
        .where(post_mine, Post.status.in_(_LIVE))
        .order_by(Post.id.desc())
        .limit(settings.learning_examples)
    )
    edits += [Edit(before=b, after=a, lang="he") for b, a in pairs]
    edits = edits[: settings.learning_examples]

    since = _since(settings)
    # Still-rejected only: an Almost later reversed to approve is an example, not a miss.
    misses = await session.execute(
        select(Post.caption_he, Feedback.reason)
        .join(Post, Feedback.post_id == Post.id)
        .where(
            fb_mine,
            Feedback.decision == Decision.ALMOST,
            Feedback.created_at >= since,
            Post.status == PostStatus.REJECTED,
        )
        .order_by(Feedback.id.desc())
        .limit(settings.learning_examples)
    )
    near_misses = [NearMiss(caption_he=c, reason=r) for c, r in misses]

    reasons = await session.scalars(
        select(Feedback.reason).where(
            fb_mine,
            Feedback.decision == Decision.REJECT,
            Feedback.reason.in_(list(_HINTS)),
            Feedback.created_at >= since,
        )
    )
    reject_hints = dict(Counter(reasons))

    recent = list(
        await session.scalars(
            select(Post.caption_he)
            .where(post_mine, Post.status == PostStatus.PUBLISHED)
            .order_by(Post.published_at.desc(), Post.id.desc())
            .limit(settings.learning_recent)
        )
    )
    return LearningContext(
        examples=examples,
        recent=recent,
        reject_hints=reject_hints,
        edits=edits,
        near_misses=near_misses,
    )


def _since(settings: Settings) -> datetime:
    return datetime.now(UTC) - timedelta(days=settings.learning_window_days)


async def verdict_counts(
    session: AsyncSession, brand_id: int, settings: Settings
) -> dict[Decision, int]:
    """Posts per decision in the learning window (a ✏️ Fix re-approve counts once)."""
    rows = await session.execute(
        select(Feedback.decision, func.count(func.distinct(Feedback.post_id)))
        .where(Feedback.brand_id == brand_id, Feedback.created_at >= _since(settings))
        .group_by(Feedback.decision)
    )
    return {Decision(d): n for d, n in rows.tuples()}


def render(ctx: LearningContext | None) -> str:
    """The prompt blocks for ``ctx``; each block is omitted when empty ("" if all are)."""
    if ctx is None:
        return ""
    parts: list[str] = []
    if ctx.examples:
        body = "\n".join(
            f"{i}. HE: {e.caption_he}\n   EN: {e.caption_en}"
            + ("\n   (the owner corrected this one by hand)" if e.edited else "")
            for i, e in enumerate(ctx.examples, 1)
        )
        parts.append(
            "These are recent posts the owner approved; match their voice, don't copy "
            f"their content.\n<approved_examples>\n{body}\n</approved_examples>"
        )
    if ctx.edits:
        body = "\n".join(f"- [{e.lang}] before: {e.before}\n  after: {e.after}" for e in ctx.edits)
        parts.append(
            "The owner rewrote these captions before approving; learn what they changed."
            f"\n<owner_corrections>\n{body}\n</owner_corrections>"
        )
    if ctx.near_misses:
        body = "\n".join(
            f"- {m.caption_he}" + (f"  (what was off: {m.reason})" if m.reason else "")
            for m in ctx.near_misses
        )
        parts.append(
            "The owner liked the direction of these but did not post them; learn the "
            f"direction, fix the flaw.\n<near_misses>\n{body}\n</near_misses>"
        )
    if ctx.reject_hints:
        lines = [
            f"- Recent rejections cited {'repeatedly ' if n >= _REPEATEDLY else ''}{_HINTS[r]}"
            for r, n in ctx.reject_hints.items()
        ]
        parts.append("Feedback from recent rejections:\n" + "\n".join(lines))
    if ctx.recent:
        body = "\n".join(f"- {c}" for c in ctx.recent)
        parts.append(
            "These were published recently; don't repeat their themes, jokes, or openings."
            f"\n<recent_posts>\n{body}\n</recent_posts>"
        )
    if ctx.retry_of:
        r = ctx.retry_of
        off = f"\nWhat was off: {r.reason}" if r.reason else ""
        parts.append(
            "You already captioned this image once. The owner liked this direction; it wasn't "
            "good enough — try again, different execution."
            f"\n<retry_of>\n{r.caption_he}{off}\n</retry_of>"
        )
    return "\n\n".join(parts)

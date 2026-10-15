"""Stage 2 — review & feedback loop.

Sends suggestions to the reviewer via a ``Notifier`` and records each decision as a
``Feedback`` row (the training signal). Approvals flow into the queue (stage 3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import learning, llm
from app.brand import load_brand
from app.config import Settings
from app.models import Decision, Feedback, Post, PostStatus
from app.notifier.base import Notifier
from app.pipeline import queue
from app.render import image_path

logger = logging.getLogger(__name__)


async def send_for_review(posts: Sequence[Post], notifier: Notifier) -> None:
    """DM every suggestion to the reviewer with Approve/Reject controls."""
    for post in posts:
        try:
            await notifier.send_suggestion(post)
        except Exception:
            logger.exception("Failed to send post %s for review.", post.id)


_TERMINAL = {PostStatus.PUBLISHING, PostStatus.PUBLISHED, PostStatus.FAILED}


# verdict → (Feedback.decision, resulting status). 📚 Keep is an approval signal; its
# status is what keeps it off the queue. ponytail: no Decision.KEEP — learning wants
# "owner liked it", and status == kept already says "didn't post".
_VERDICTS = {
    "approve": (Decision.APPROVE, PostStatus.APPROVED),
    "keep": (Decision.APPROVE, PostStatus.KEPT),
    "reject": (Decision.REJECT, PostStatus.REJECTED),
    "almost": (Decision.ALMOST, PostStatus.REJECTED),
}


async def handle_decision(
    session: AsyncSession,
    post_id: int,
    decision: str,
    reason: str | None = None,
    edit: tuple[str, str, str] | None = None,
) -> Post | None:
    """Apply a verdict (``approve`` / ``keep`` / ``reject`` / ``almost``) to a pre-publish post.

    - SUGGESTED, APPROVED, KEPT, REJECTED → any verdict moves the post (reversals allowed);
      ``approve`` enqueues, everything else clears the queue position.
    - Verdict whose status the post already has → no-op (double-tap guard).
    - PUBLISHING/PUBLISHED/FAILED → never mutated (approval gate is load-bearing).
    Every real transition writes a Feedback row (``edit`` = (lang, before, after) when
    the owner corrected a caption on the way — see ``apply_edit``).
    """
    post = await session.get(Post, post_id)
    if post is None:
        logger.warning("Decision for unknown post %s ignored.", post_id)
        return None

    if post.status in _TERMINAL:
        logger.info("Post %s is %s — cannot reverse.", post_id, post.status)
        return post

    dec, status = _VERDICTS[decision]
    if post.status == status:
        logger.info("Post %s already %s — no-op.", post_id, post.status)
        return post

    session.add(Feedback(post_id=post.id, decision=dec, reason=reason, **_edit_cols(edit)))
    post.decided_at = datetime.now(UTC)

    if status is PostStatus.APPROVED:
        await queue.enqueue(session, post)
    else:
        post.status = status
        post.queue_position = None  # off the queue if reversing an approval

    await session.commit()
    await session.refresh(post)
    logger.info("Post %s -> %s.", post_id, post.status)
    return post


async def retry_child(session: AsyncSession, post_id: int) -> Post | None:
    """The 🔁 retry already generated for ``post_id``, if any."""
    return await session.scalar(select(Post).where(Post.retry_of == post_id).limit(1))


async def retry(session: AsyncSession, post_id: int, settings: Settings) -> Post | None:
    """🔁 Retry a 🤏 Almost'd post: re-caption the **same image** as a new suggestion.

    Returns the new SUGGESTED post (``retry_of`` = ``post_id``), the existing one when the
    post was already retried, or None when ``post_id`` isn't an Almost'd rejected post.
    The caller sends the new post for review. Raises ``CaptionError`` if the model fails.
    """
    post = await session.get(Post, post_id)
    if post is None or post.status != PostStatus.REJECTED:
        return None
    last = await session.scalar(
        select(Feedback).where(Feedback.post_id == post_id).order_by(Feedback.id.desc()).limit(1)
    )
    if last is None or last.decision != Decision.ALMOST:
        return None
    if (existing := await retry_child(session, post_id)) is not None:
        return existing

    context = await learning.build_context(session, settings)
    context.retry_of = learning.NearMiss(caption_he=post.caption_he, reason=last.reason)
    s = await llm.caption_image(load_brand(), image_path(post, settings), settings, context)
    new = Post(
        image_ref=post.image_ref,
        caption_he=s.caption_he,
        caption_en=s.caption_en,
        visual_concept=s.visual_concept,
        rationale=s.rationale,
        status=PostStatus.SUGGESTED,
        batch_id=post.batch_id,
        retry_of=post.id,
    )
    session.add(new)
    await session.commit()
    await session.refresh(new)
    logger.info("Post %s retried as #%s.", post_id, new.id)
    return new


def _edit_cols(edit: tuple[str, str, str] | None) -> dict[str, str]:
    if edit is None:
        return {}
    lang, before, after = edit
    return {"edit_lang": lang, "edit_before": before, "edit_after": after}


async def apply_edit(session: AsyncSession, post_id: int, lang: str, text: str) -> Post | None:
    """✏️ Fix: replace the ``he``/``en`` caption and approve (M8.2).

    - SUGGESTED → caption replaced, then approved via ``handle_decision`` (the one gate).
    - APPROVED → caption replaced, queue position untouched; a Feedback row records the delta.
    - Any other status (rejected/publishing/published/failed) → returned unchanged.
    An unchanged caption is a no-op. The returned post's status tells the caller what happened.
    """
    post = await session.get(Post, post_id)
    if post is None or post.status not in (PostStatus.SUGGESTED, PostStatus.APPROVED):
        return post
    field = "caption_he" if lang == "he" else "caption_en"
    before = getattr(post, field)
    if text == before:
        return post
    edit = (lang, before, text)
    setattr(post, field, text)
    if post.status == PostStatus.SUGGESTED:
        return await handle_decision(session, post_id, "approve", edit=edit)
    session.add(Feedback(post_id=post.id, decision=Decision.APPROVE, **_edit_cols(edit)))
    await session.commit()
    await session.refresh(post)
    return post

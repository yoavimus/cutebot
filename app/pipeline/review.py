"""Stage 2 — review & feedback loop.

Sends suggestions to the reviewer via a ``Notifier`` and records each decision as a
``Feedback`` row (the training signal). Approvals flow into the queue (stage 3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Decision, Feedback, Post, PostStatus
from app.notifier.base import Notifier
from app.pipeline import queue

logger = logging.getLogger(__name__)


async def send_for_review(posts: Sequence[Post], notifier: Notifier) -> None:
    """DM every suggestion to the reviewer with Approve/Reject controls."""
    for post in posts:
        try:
            await notifier.send_suggestion(post)
        except Exception:
            logger.exception("Failed to send post %s for review.", post.id)


_TERMINAL = {PostStatus.PUBLISHING, PostStatus.PUBLISHED, PostStatus.FAILED}


async def handle_decision(
    session: AsyncSession, post_id: int, decision: str, reason: str | None = None
) -> Post | None:
    """Apply an approve/reject decision, allowing reversals on pre-publish posts.

    - SUGGESTED → approve/reject as normal.
    - APPROVED → can be flipped to REJECTED (removes from queue).
    - REJECTED → can be flipped to APPROVED (re-enqueues).
    - Same decision repeated → no-op (idempotent).
    - PUBLISHING/PUBLISHED/FAILED → never mutated (approval gate is load-bearing).
    Every real transition writes a Feedback row.
    """
    post = await session.get(Post, post_id)
    if post is None:
        logger.warning("Decision for unknown post %s ignored.", post_id)
        return None

    if post.status in _TERMINAL:
        logger.info("Post %s is %s — cannot reverse.", post_id, post.status)
        return post

    dec = Decision(decision)

    # Same decision → no-op (double-tap guard)
    if (dec is Decision.APPROVE and post.status == PostStatus.APPROVED) or (
        dec is Decision.REJECT and post.status == PostStatus.REJECTED
    ):
        logger.info("Post %s already %s — no-op.", post_id, post.status)
        return post

    session.add(Feedback(post_id=post.id, decision=dec, reason=reason))
    post.decided_at = datetime.now(UTC)

    if dec is Decision.APPROVE:
        await queue.enqueue(session, post)
    else:
        post.status = PostStatus.REJECTED
        post.queue_position = None  # remove from queue if reversing an approval

    await session.commit()
    await session.refresh(post)
    logger.info("Post %s -> %s.", post_id, post.status)
    return post

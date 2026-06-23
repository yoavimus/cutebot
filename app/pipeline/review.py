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
        await notifier.send_suggestion(post)


async def handle_decision(session: AsyncSession, post_id: int, decision: str) -> Post | None:
    """Apply an approve/reject decision: record feedback and advance state.

    Idempotent w.r.t. already-decided posts — a second decision on the same post is
    ignored (returns the post unchanged) so a double-tap can't corrupt the queue.
    """
    post = await session.get(Post, post_id)
    if post is None:
        logger.warning("Decision for unknown post %s ignored.", post_id)
        return None
    if post.status != PostStatus.SUGGESTED:
        logger.info("Post %s already %s — ignoring %s.", post_id, post.status, decision)
        return post

    dec = Decision(decision)
    session.add(Feedback(post_id=post.id, decision=dec))
    post.decided_at = datetime.now(UTC)

    if dec is Decision.APPROVE:
        await queue.enqueue(session, post)
    else:
        post.status = PostStatus.REJECTED

    await session.commit()
    await session.refresh(post)
    logger.info("Post %s -> %s.", post_id, post.status)
    return post

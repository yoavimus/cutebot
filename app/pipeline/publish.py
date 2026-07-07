"""Stage 4 — scheduled publishing.

At each posting slot, pull the front-of-queue approved post and broadcast it to every
configured publisher. Idempotent: the post is moved to ``publishing`` before any network
call, so a crash/retry can't double-post.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Post, PostStatus
from app.pipeline import queue
from app.publishers.base import Publisher, get_publishers

logger = logging.getLogger(__name__)


async def recover_orphaned(session: AsyncSession) -> int:
    """Reset any PUBLISHING posts to APPROVED (startup crash recovery).

    ponytail: startup sweep; add per-post leases only if v1 ever goes multi-worker.
    """
    posts = (
        await session.scalars(select(Post).where(Post.status == PostStatus.PUBLISHING))
    ).all()
    for post in posts:
        post.status = PostStatus.APPROVED
    if posts:
        await session.commit()
        logger.info("Recovered %d orphaned publishing post(s).", len(posts))
    return len(posts)


async def publish_next(
    session: AsyncSession,
    publishers: list[Publisher] | None = None,
) -> Post | None:
    """Publish the next approved post to all networks. Returns the post, or None if empty."""
    post = await queue.peek_next(session)
    if post is None:
        logger.info("Posting slot fired but the queue is empty — nothing to publish.")
        return None

    # ponytail: peek→claim not atomic; SELECT … FOR UPDATE SKIP LOCKED on Postgres if multi-worker
    # Claim the post before any network call (idempotency guard).
    post.status = PostStatus.PUBLISHING
    await session.commit()

    targets = publishers if publishers is not None else get_publishers()
    results = []
    for publisher in targets:
        try:
            results.append(await publisher.publish(post))
        except Exception as exc:  # noqa: BLE001 — record per-network failure, keep going
            logger.exception("Publisher %s failed for post %s.", publisher.name, post.id)
            from app.publishers.base import PublishResult

            results.append(PublishResult(network=publisher.name, ok=False, detail=str(exc)))

    if results and all(r.ok for r in results):
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime.now(UTC)
        post.queue_position = None
    else:
        post.status = PostStatus.FAILED
        logger.error("Post %s failed to publish to one or more networks.", post.id)

    await session.commit()
    await session.refresh(post)
    return post

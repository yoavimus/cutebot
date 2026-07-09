"""Stage 4 — scheduled publishing.

At each posting slot, pull the front-of-queue approved post and broadcast it to every
configured publisher. Idempotent: the post is moved to ``publishing`` before any network
call, so a crash/retry can't double-post.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Post, PostStatus
from app.pipeline import queue
from app.publishers.base import Publisher, PublishResult, get_publishers

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


async def _do_publish(
    session: AsyncSession,
    post: Post,
    publishers: list[Publisher] | None = None,
) -> Post:
    """Claim and publish post. Caller ensures post is APPROVED."""
    # ponytail: peek→claim not atomic; SELECT … FOR UPDATE SKIP LOCKED on Postgres if multi-worker
    post.status = PostStatus.PUBLISHING
    await session.commit()

    targets = publishers if publishers is not None else get_publishers()
    results = []
    for publisher in targets:
        try:
            results.append(await publisher.publish(post))
        except Exception as exc:  # noqa: BLE001 — record per-network failure, keep going
            logger.exception("Publisher %s failed for post %s.", publisher.name, post.id)
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


async def publish_next(
    session: AsyncSession,
    publishers: list[Publisher] | None = None,
) -> Post | None:
    """Publish the next approved post to all networks. Returns the post, or None if empty."""
    post = await queue.peek_next(session)
    if post is None:
        logger.info("Posting slot fired but the queue is empty — nothing to publish.")
        return None
    return await _do_publish(session, post, publishers)


async def catch_up_missed_slot(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    publishers: list[Publisher] | None = None,
) -> Post | None:
    """Publish once at startup if a posting slot was missed while the process was down.

    misfire_grace_time only covers delays while the scheduler is alive; the in-memory
    jobstore can't backfill runs missed across a restart/redeploy. So: if the most
    recent slot fired within ``CATCHUP_WINDOW_MIN`` and nothing was published since,
    drain one post now. ponytail: startup check, not a persistent jobstore.
    """
    slots = settings.posting_slots_list
    if not slots:
        return None
    tz = ZoneInfo(settings.schedule_tz)
    now = (now or datetime.now(tz)).astimezone(tz)
    slot_times = []
    for hh, mm in slots:
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot > now:
            slot -= timedelta(days=1)
        slot_times.append(slot)
    last_slot = max(slot_times)
    if now - last_slot > timedelta(minutes=settings.catchup_window_min):
        return None

    last_published = await session.scalar(select(func.max(Post.published_at)))
    if last_published is not None:
        if last_published.tzinfo is None:  # SQLite returns naive datetimes; stored as UTC
            last_published = last_published.replace(tzinfo=UTC)
        if last_published >= last_slot:
            return None

    logger.info("Posting slot at %s was missed (restart?) — catching up.", last_slot)
    return await publish_next(session, publishers)


async def publish_by_id(
    session: AsyncSession,
    post_id: int,
    publishers: list[Publisher] | None = None,
) -> Post | None:
    """Publish a specific approved post by ID. Returns post unchanged (not None) if not APPROVED."""
    post = await session.get(Post, post_id)
    if post is None or post.status != PostStatus.APPROVED:
        return post
    return await _do_publish(session, post, publishers)

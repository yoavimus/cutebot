"""Stage 3 — the approved-post queue.

One ordered queue per brand: approved posts get a monotonically increasing
``queue_position``; publishing drains from the front (lowest position).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Post, PostStatus


async def _max_position(session: AsyncSession, brand_id: int) -> int | None:
    return await session.scalar(
        select(func.max(Post.queue_position)).where(
            Post.brand_id == brand_id, Post.status == PostStatus.APPROVED
        )
    )


async def enqueue(session: AsyncSession, post: Post) -> None:
    """Mark a post approved and append it to the back of the queue.

    Does not commit — the caller owns the transaction boundary.
    """
    max_pos = await _max_position(session, post.brand_id)
    post.status = PostStatus.APPROVED
    post.queue_position = (max_pos or 0) + 1


async def peek_next(session: AsyncSession, brand_id: int) -> Post | None:
    """Return the brand's front-of-queue approved post (lowest position), or None."""
    return await session.scalar(
        select(Post)
        .where(Post.brand_id == brand_id, Post.status == PostStatus.APPROVED)
        .order_by(Post.queue_position.asc())
        .limit(1)
    )


async def requeue(session: AsyncSession, post: Post) -> None:
    """Move a FAILED post to the back of the queue. Caller owns the commit."""
    if post.status != PostStatus.FAILED:
        return
    max_pos = await _max_position(session, post.brand_id)
    post.status = PostStatus.APPROVED
    post.queue_position = (max_pos or 0) + 1


async def queue_length(session: AsyncSession, brand_id: int) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(Post)
        .where(Post.brand_id == brand_id, Post.status == PostStatus.APPROVED)
    )
    return int(count or 0)

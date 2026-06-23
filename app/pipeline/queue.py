"""Stage 3 — the approved-post queue.

A simple ordered queue: approved posts get a monotonically increasing
``queue_position``; publishing drains from the front (lowest position).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Post, PostStatus


async def enqueue(session: AsyncSession, post: Post) -> None:
    """Mark a post approved and append it to the back of the queue.

    Does not commit — the caller owns the transaction boundary.
    """
    max_pos = await session.scalar(
        select(func.max(Post.queue_position)).where(Post.status == PostStatus.APPROVED)
    )
    post.status = PostStatus.APPROVED
    post.queue_position = (max_pos or 0) + 1


async def peek_next(session: AsyncSession) -> Post | None:
    """Return the front-of-queue approved post (lowest position), or None."""
    return await session.scalar(
        select(Post)
        .where(Post.status == PostStatus.APPROVED)
        .order_by(Post.queue_position.asc())
        .limit(1)
    )


async def queue_length(session: AsyncSession) -> int:
    count = await session.scalar(
        select(func.count()).select_from(Post).where(Post.status == PostStatus.APPROVED)
    )
    return int(count or 0)

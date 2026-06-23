"""Stage 1 — batch generation.

Reads brand guidelines, asks the LLM agent for N suggestions, and persists them as
``Post`` rows in ``suggested`` status plus a ``Batch`` provenance row.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app import llm
from app.brand import load_brand
from app.config import get_settings
from app.models import Batch, Post, PostStatus

logger = logging.getLogger(__name__)


async def generate_batch(
    session: AsyncSession,
    *,
    n: int | None = None,
    brand: str | None = None,
) -> list[Post]:
    """Generate and store a batch of suggestions. Returns the created posts."""
    settings = get_settings()
    size = n or settings.batch_size
    brand_text = brand if brand is not None else load_brand()

    suggestions = await llm.generate_suggestions(brand_text, size)

    batch = Batch(
        model=settings.default_llm_model,
        size=len(suggestions),
        brand_snapshot=brand_text,
    )
    session.add(batch)
    await session.flush()  # assign batch.id

    posts: list[Post] = []
    for s in suggestions:
        post = Post(
            caption=s.caption,
            visual_concept=s.visual_concept,
            rationale=s.rationale,
            status=PostStatus.SUGGESTED,
            batch_id=batch.id,
        )
        session.add(post)
        posts.append(post)

    await session.commit()
    for post in posts:
        await session.refresh(post)
    logger.info("Generated batch %s with %d suggestions.", batch.id, len(posts))
    return posts

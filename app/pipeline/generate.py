"""Stage 1 — batch generation.

Image-first (PRODUCT_SPEC §3): pick images from the stock library, then caption each
one (vision, bilingual) — never the reverse. Persists ``Post`` rows in ``suggested``
status plus a ``Batch`` provenance row.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app import llm, stock
from app.brand import load_brand
from app.config import get_settings
from app.llm import CaptionError
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

    images = await stock.select_images(session, size, settings)

    batch = Batch(
        model=settings.default_llm_model,
        size=len(images),
        brand_snapshot=brand_text,
    )
    session.add(batch)
    await session.flush()  # assign batch.id

    stock_dir = settings.stock_images_dir
    posts: list[Post] = []
    for image in images:
        try:
            s = await llm.caption_image(brand_text, image, settings)
        except CaptionError as exc:
            logger.warning("Skipping image %s: %s", image.name, exc)
            continue
        ref = str(image.relative_to(stock_dir)) if image.is_relative_to(stock_dir) else str(image)
        post = Post(
            image_ref=ref,
            caption_he=s.caption_he,
            caption_en=s.caption_en,
            visual_concept=s.visual_concept,
            rationale=s.rationale,
            status=PostStatus.SUGGESTED,
            batch_id=batch.id,
        )
        session.add(post)
        posts.append(post)

    batch.size = len(posts)  # actual successes, not planned
    await session.commit()
    for post in posts:
        await session.refresh(post)
    logger.info("Generated batch %s with %d suggestions.", batch.id, len(posts))
    return posts

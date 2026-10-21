"""Stage 1 — batch generation.

Image-first (PRODUCT_SPEC §3): pick images from the stock library, then caption each
one (vision, bilingual) — never the reverse. Persists ``Post`` rows in ``suggested``
status plus a ``Batch`` provenance row.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app import brands, learning, llm, stock
from app.config import get_settings
from app.llm import CaptionError
from app.models import Batch, Brand, Post, PostStatus

logger = logging.getLogger(__name__)


async def generate_batch(
    session: AsyncSession,
    brand: Brand,
    *,
    n: int | None = None,
) -> list[Post]:
    """Generate and store a batch of suggestions for ``brand``. Returns the created posts.

    Per-post tokens and cost come from the LiteLLM usage on each suggestion; the batch
    stores the sum (None when no post has a priced usage — never a guessed 0).
    """
    settings = get_settings()
    size = n or settings.batch_size
    brand_text = brands.text(brand)

    context = await learning.build_context(session, brand.id, settings)
    images = await stock.select_images(session, brand, size, settings)

    batch = Batch(
        brand_id=brand.id,
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
            s = await llm.caption_image(brand_text, image, settings, context)
        except CaptionError as exc:
            logger.warning("Skipping image %s: %s", image.name, exc)
            continue
        ref = str(image.relative_to(stock_dir)) if image.is_relative_to(stock_dir) else str(image)
        cost = llm.cost_usd(settings.default_llm_model, s.tokens_in, s.tokens_out)
        post = Post(
            brand_id=brand.id,
            image_ref=ref,
            caption_he=s.caption_he,
            caption_en=s.caption_en,
            visual_concept=s.visual_concept,
            rationale=s.rationale,
            tokens_in=s.tokens_in,
            tokens_out=s.tokens_out,
            cost_usd=None if cost is None else Decimal(f"{cost:.6f}"),
            status=PostStatus.SUGGESTED,
            batch_id=batch.id,
        )
        session.add(post)
        posts.append(post)

    batch.size = len(posts)  # actual successes, not planned
    costs = [p.cost_usd for p in posts if p.cost_usd is not None]
    batch.cost_usd = sum(costs, Decimal(0)) if costs else None
    await session.commit()
    for post in posts:
        await session.refresh(post)
    logger.info("Generated batch %s with %d suggestions.", batch.id, len(posts))
    return posts

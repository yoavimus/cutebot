"""The owner-provided stock image library — see PRODUCT_SPEC §3.

Generation is image-first: pick an image from here, then caption it (vision).
``image_ref`` on ``Post`` is always the path relative to ``settings.stock_images_dir``.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import random
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Post, PostStatus

logger = logging.getLogger(__name__)

_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def list_images(settings: Settings) -> list[Path]:
    """Enumerate stock images under ``settings.stock_images_dir`` (sorted, deterministic)."""
    stock_dir = Path(settings.stock_images_dir)
    if not stock_dir.is_dir():
        return []
    return sorted(p for p in stock_dir.iterdir() if p.suffix.lower() in _EXTENSIONS)


_COMMITTED = {PostStatus.APPROVED, PostStatus.PUBLISHING, PostStatus.PUBLISHED}


async def select_images(session: AsyncSession, n: int, settings: Settings) -> list[Path]:
    """Pick ``n`` images randomly, preferring ones not tied to committed posts.

    "Committed" = APPROVED/PUBLISHING/PUBLISHED. Rejected and suggested images are
    free to reuse. If the uncommitted pool runs dry, cycles from committed images.
    Returns fewer than ``n`` only if the stock library is totally empty.
    """
    stock_dir = Path(settings.stock_images_dir)
    images = list_images(settings)
    if not images:
        logger.warning("Stock library %s is empty — no images to select.", stock_dir)
        return []

    committed_refs = set(
        (await session.scalars(select(Post.image_ref).where(Post.status.in_(_COMMITTED)))).all()
    )
    unused = [p for p in images if str(p.relative_to(stock_dir)) not in committed_refs]
    committed = [p for p in images if str(p.relative_to(stock_dir)) in committed_refs]

    selected = random.sample(unused, min(n, len(unused)))
    if len(selected) < n:
        shortfall = n - len(selected)
        cycle = committed if committed else images
        if committed:
            logger.warning(
                "Stock library exhausted — all committed images in use. Add more images."
            )
        selected += [cycle[i % len(cycle)] for i in range(shortfall)]
    return selected


def load_image_b64(path: Path) -> tuple[str, str]:
    """Return ``(mime_type, base64_str)`` for the vision call."""
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type is None:
        mime_type = "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return mime_type, data

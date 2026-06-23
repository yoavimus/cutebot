"""The owner-provided stock image library — see PRODUCT_SPEC §3.

Generation is image-first: pick an image from here, then caption it (vision).
``image_ref`` on ``Post`` is always the path relative to ``settings.stock_images_dir``.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Post

logger = logging.getLogger(__name__)

_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def list_images(settings: Settings) -> list[Path]:
    """Enumerate stock images under ``settings.stock_images_dir`` (sorted, deterministic)."""
    stock_dir = Path(settings.stock_images_dir)
    if not stock_dir.is_dir():
        return []
    return sorted(p for p in stock_dir.iterdir() if p.suffix.lower() in _EXTENSIONS)


async def select_images(session: AsyncSession, n: int, settings: Settings) -> list[Path]:
    """Pick ``n`` images, preferring ones not yet referenced by any ``Post`` (rotation).

    If the unused pool is smaller than ``n``, tops up by cycling already-used images.
    Returns fewer than ``n`` only if the stock library has fewer than ``n`` images total.
    """
    stock_dir = Path(settings.stock_images_dir)
    images = list_images(settings)
    if not images:
        logger.warning("Stock library %s is empty — no images to select.", stock_dir)
        return []

    used_refs = set((await session.scalars(select(Post.image_ref))).all())
    unused = [p for p in images if str(p.relative_to(stock_dir)) not in used_refs]
    used = [p for p in images if str(p.relative_to(stock_dir)) in used_refs]

    selected = unused[:n]
    if len(selected) < n:
        shortfall = n - len(selected)
        cycle = used if used else images
        selected += [cycle[i % len(cycle)] for i in range(shortfall)]
    return selected


def load_image_b64(path: Path) -> tuple[str, str]:
    """Return ``(mime_type, base64_str)`` for the vision call."""
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type is None:
        mime_type = "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return mime_type, data

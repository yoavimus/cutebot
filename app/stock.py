"""The owner-provided stock image library — see PRODUCT_SPEC §3.

Generation is image-first: pick an image from here, then caption it (vision).
``image_ref`` on ``Post`` is always the path relative to ``settings.stock_images_dir``.
"""

from __future__ import annotations

import base64
import io
import logging
import random
from pathlib import Path

from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import BannedImage, Post, PostStatus

logger = logging.getLogger(__name__)

_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def list_images(settings: Settings) -> list[Path]:
    """Enumerate stock images under ``settings.stock_images_dir`` (sorted, deterministic)."""
    stock_dir = Path(settings.stock_images_dir)
    if not stock_dir.is_dir():
        return []
    return sorted(p for p in stock_dir.iterdir() if p.suffix.lower() in _EXTENSIONS)


_COMMITTED = {PostStatus.APPROVED, PostStatus.PUBLISHING, PostStatus.PUBLISHED}


async def _pools(
    session: AsyncSession, settings: Settings
) -> tuple[list[Path], set[str], set[str]]:
    """All images on disk + the refs that are committed (used) and banned."""
    images = list_images(settings)
    committed = set(
        (await session.scalars(select(Post.image_ref).where(Post.status.in_(_COMMITTED)))).all()
    )
    banned = set((await session.scalars(select(BannedImage.image_ref))).all())
    return images, committed, banned


async def select_images(session: AsyncSession, n: int, settings: Settings) -> list[Path]:
    """Pick up to ``n`` random images that are neither used by a committed post nor banned.

    "Committed" = APPROVED/PUBLISHING/PUBLISHED. Rejected and suggested images are free
    to reuse. Never recycles a used image: when the pool runs dry it returns fewer than
    ``n`` and the caller tells the owner to upload more (see ``counts``).
    """
    stock_dir = Path(settings.stock_images_dir)
    images, committed, banned = await _pools(session, settings)
    if not images:
        logger.warning("Stock library %s is empty — no images to select.", stock_dir)
        return []
    unused = [p for p in images if str(p.relative_to(stock_dir)) not in committed | banned]
    if len(unused) < n:
        logger.warning("Stock library low: %d unused image(s), %d requested.", len(unused), n)
    return random.sample(unused, min(n, len(unused)))


async def counts(session: AsyncSession, settings: Settings) -> dict[str, int]:
    """``{"unused", "used", "banned", "total"}`` for /status and /stock."""
    stock_dir = Path(settings.stock_images_dir)
    images, committed, banned = await _pools(session, settings)
    refs = {str(p.relative_to(stock_dir)) for p in images}
    return {
        "unused": len(refs - committed - banned),
        "used": len(refs & committed),
        "banned": len(refs & banned),
        "total": len(refs),
    }


def summary_line(c: dict[str, int]) -> str:
    return (
        f"Stock: {c['unused']} unused / {c['used']} used / {c['banned']} banned "
        f"({c['total']} files)"
    )


async def low_stock_message(
    session: AsyncSession, settings: Settings, got: int, wanted: int
) -> str | None:
    """Owner-facing text when a batch came back short because the unused pool ran dry."""
    if got >= wanted:
        return None
    c = await counts(session, settings)
    return (
        f"Only {got} of {wanted} generated — {summary_line(c)}.\n"
        "Send photos to this chat to add them to the stock library."
    )


async def ban_image(session: AsyncSession, image_ref: str) -> None:
    """Idempotent: a second ban of the same ref is a no-op."""
    if await session.get(BannedImage, image_ref) is None:
        session.add(BannedImage(image_ref=image_ref))
        await session.flush()


async def unban_image(session: AsyncSession, image_ref: str) -> bool:
    row = await session.get(BannedImage, image_ref)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


def load_image_b64(path: Path, max_edge: int = 1568) -> tuple[str, str]:
    """Return ``("image/jpeg", base64_str)`` for the vision call.

    Downscales to ``max_edge`` on the long side and re-encodes JPEG so the base64
    payload stays well under provider limits — Claude rejects base64 images >10MB, and
    real stock (4000px phone photos) blows past that. 1568px is Anthropic's recommended
    vision long-edge; larger just burns tokens without adding detail.
    """
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge))  # shrinks only, preserves aspect
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
    return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")

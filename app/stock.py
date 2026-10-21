"""The owner-provided stock image library — see PRODUCT_SPEC §3.

Generation is image-first: pick an image from here, then caption it (vision).
``image_ref`` on ``Post`` is always the path relative to ``settings.stock_images_dir``. Each
brand reads ``stock/<stock_prefix>/`` (empty prefix = the top folder, brand 1 today), so
its refs carry the prefix and ``/media`` serves them unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import random
from pathlib import Path

import pillow_heif
from PIL import Image
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import BannedImage, Brand, Post, PostStatus, StockImage

logger = logging.getLogger(__name__)

_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_HEIC = {".heic", ".heif"}
IMPORTABLE = _EXTENSIONS | _HEIC  # what an upload may be; HEIC is converted on register
pillow_heif.register_heif_opener()


def brand_dir(settings: Settings, prefix: str) -> Path:
    return Path(settings.stock_images_dir) / prefix


def list_images(settings: Settings, prefix: str) -> list[Path]:
    """Enumerate one brand's stock images (sorted, deterministic). ``prefix`` = stock_prefix."""
    stock_dir = brand_dir(settings, prefix)
    if not stock_dir.is_dir():
        return []
    return sorted(p for p in stock_dir.iterdir() if p.suffix.lower() in _EXTENSIONS)


# KEPT holds its image too: a kept post can still be promoted to approved and published.
_COMMITTED = {PostStatus.APPROVED, PostStatus.KEPT, PostStatus.PUBLISHING, PostStatus.PUBLISHED}


async def _pools(
    session: AsyncSession, brand: Brand, settings: Settings
) -> tuple[list[Path], set[str], set[str]]:
    """The brand's images on disk + its refs that are committed (used) and banned."""
    images = list_images(settings, brand.stock_prefix)
    held = or_(
        Post.status.in_(_COMMITTED),
        # a pending 🔁 retry reuses its Almost'd parent's image — not back in the pool yet
        and_(Post.status == PostStatus.SUGGESTED, Post.retry_of.is_not(None)),
    )
    mine = Post.brand_id == brand.id
    committed = set((await session.scalars(select(Post.image_ref).where(mine, held))).all())
    banned = set(
        (
            await session.scalars(
                select(BannedImage.image_ref).where(BannedImage.brand_id == brand.id)
            )
        ).all()
    )
    return images, committed, banned


async def select_images(
    session: AsyncSession, brand: Brand, n: int, settings: Settings
) -> list[Path]:
    """Pick up to ``n`` random images that are neither used by a committed post nor banned.

    "Committed" = APPROVED/KEPT/PUBLISHING/PUBLISHED, plus a suggested 🔁 retry. Other
    rejected and suggested images are free to reuse. Never recycles a used image: when the
    pool runs dry it returns fewer than ``n`` and the caller tells the owner to upload more
    (see ``counts``).
    """
    stock_dir = Path(settings.stock_images_dir)
    images, committed, banned = await _pools(session, brand, settings)
    if not images:
        empty = brand_dir(settings, brand.stock_prefix)
        logger.warning("Stock library %s is empty — no images to select.", empty)
        return []
    unused = [p for p in images if str(p.relative_to(stock_dir)) not in committed | banned]
    if len(unused) < n:
        logger.warning("Stock library low: %d unused image(s), %d requested.", len(unused), n)
    return random.sample(unused, min(n, len(unused)))


async def counts(session: AsyncSession, brand: Brand, settings: Settings) -> dict[str, int]:
    """``{"unused", "used", "banned", "total"}`` for /status and /stock."""
    stock_dir = Path(settings.stock_images_dir)
    images, committed, banned = await _pools(session, brand, settings)
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
    session: AsyncSession, brand: Brand, settings: Settings, got: int, wanted: int
) -> str | None:
    """Owner-facing text when a batch came back short because the unused pool ran dry."""
    if got >= wanted:
        return None
    c = await counts(session, brand, settings)
    return (
        f"Only {got} of {wanted} generated — {summary_line(c)}.\n"
        "Send photos to this chat to add them to the stock library (send as file for full quality)."
    )


async def ban_image(session: AsyncSession, brand_id: int, image_ref: str) -> None:
    """Idempotent: a second ban of the same ref is a no-op."""
    if await session.get(BannedImage, (brand_id, image_ref)) is None:
        session.add(BannedImage(brand_id=brand_id, image_ref=image_ref))
        await session.flush()


async def unban_image(session: AsyncSession, brand_id: int, image_ref: str) -> bool:
    row = await session.get(BannedImage, (brand_id, image_ref))
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


def unique_path(directory: Path, name: str) -> Path:
    """``directory/name``, suffixed ``-2``, ``-3``… when taken (an upload never overwrites)."""
    base = Path(name).name
    dest, n = directory / base, 2
    while dest.exists():
        dest = directory / f"{Path(base).stem}-{n}{Path(base).suffix}"
        n += 1
    return dest


async def register(
    session: AsyncSession, brand: Brand, path: Path, source: str, source_id: str | None
) -> StockImage | None:
    """The one way a file enters the library (``path`` is already inside the brand's dir).

    Hashes the file as received; a hash the brand already has → the new file is removed and
    ``None`` returned. HEIC/HEIF is converted to JPEG in place. Caller commits.
    """
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    dup = await session.scalar(
        select(StockImage.image_ref).where(
            StockImage.brand_id == brand.id,
            StockImage.sha256 == sha,
            StockImage.deleted_at.is_(None),
        )
    )
    if dup is not None:
        path.unlink()
        return None
    if path.suffix.lower() in _HEIC:
        jpeg = unique_path(path.parent, path.stem + ".jpg")
        with Image.open(path) as img:
            img.convert("RGB").save(jpeg, format="JPEG", quality=92)
        path.unlink()
        path = jpeg
    ref = path.name if not brand.stock_prefix else f"{brand.stock_prefix.rstrip('/')}/{path.name}"
    row = StockImage(
        brand_id=brand.id, image_ref=ref, source=source, source_id=source_id, sha256=sha
    )
    await session.merge(row)  # a deleted/re-imported ref replaces its old row
    await session.flush()
    return row


async def backfill_manual(session: AsyncSession, brand: Brand, settings: Settings) -> int:
    """Give every on-disk file with no row a ``manual`` row. Returns how many were added."""
    root = Path(settings.stock_images_dir)
    have = set(
        await session.scalars(select(StockImage.image_ref).where(StockImage.brand_id == brand.id))
    )
    added = 0
    for p in list_images(settings, brand.stock_prefix):
        ref = str(p.relative_to(root))
        if ref not in have:
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            session.add(StockImage(brand_id=brand.id, image_ref=ref, source="manual", sha256=sha))
            added += 1
    await session.flush()
    return added

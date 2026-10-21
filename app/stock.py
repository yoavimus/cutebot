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
import os
import random
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pillow_heif
from PIL import Image
from sqlalchemy import and_, func, or_, select
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


# ─────────────────────────────── browser (M11.2) ───────────────────────────────

PAGE_SIZE = 10
TRASH_DAYS = 30
# A delete must not pull the file from under a post that still has to be sent or published.
_NEEDS_FILE = {PostStatus.SUGGESTED, PostStatus.APPROVED, PostStatus.PUBLISHING}


def ref_hash(ref: str) -> str:
    """Short stable id of a ref — fits Telegram's 64-byte callback data, unlike the ref."""
    return hashlib.sha1(ref.encode()).hexdigest()[:10]  # noqa: S324 — an id, not security


def brand_refs(settings: Settings, brand: Brand) -> list[str]:
    root = Path(settings.stock_images_dir)
    return [str(p.relative_to(root)) for p in list_images(settings, brand.stock_prefix)]


@dataclass
class PageItem:
    ref: str
    used: int
    last_used: datetime | None
    banned: bool


async def page_items(
    session: AsyncSession, brand: Brand, settings: Settings, page: int
) -> tuple[list[PageItem], int, int]:
    """``(items, page, pages)`` — ``page`` is clamped into range; empty library = ([], 0, 0)."""
    refs = brand_refs(settings, brand)
    pages = -(-len(refs) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    window = refs[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    if not window:
        return [], 0, 0
    when = func.coalesce(Post.published_at, Post.decided_at, Post.created_at)
    usage = {
        r: (n, last)
        for r, n, last in (
            await session.execute(
                select(Post.image_ref, func.count(), func.max(when))
                .where(
                    Post.brand_id == brand.id,
                    Post.image_ref.in_(window),
                    Post.status.in_(_COMMITTED),
                )
                .group_by(Post.image_ref)
            )
        ).all()
    }
    banned = set(
        (
            await session.scalars(
                select(BannedImage.image_ref).where(
                    BannedImage.brand_id == brand.id, BannedImage.image_ref.in_(window)
                )
            )
        ).all()
    )
    items = [PageItem(r, *usage.get(r, (0, None)), r in banned) for r in window]
    return items, page, pages


def thumbnail(path: Path, edge: int = 320) -> bytes:
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((edge, edge))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def trash_dir(settings: Settings, brand: Brand) -> Path:
    return Path(settings.stock_images_dir) / ".trash" / str(brand.id)


async def delete_image(
    session: AsyncSession, brand: Brand, settings: Settings, ref: str
) -> str | None:
    """Move the file to ``.trash`` and mark its row deleted. Returns None on success, else
    a short reason ("missing" / "in use"). Caller commits."""
    path = Path(settings.stock_images_dir) / ref
    if ref not in brand_refs(settings, brand):
        return "missing"
    held = await session.scalar(
        select(func.count()).where(
            Post.brand_id == brand.id, Post.image_ref == ref, Post.status.in_(_NEEDS_FILE)
        )
    )
    if held:
        return "in use"
    await backfill_manual(session, brand, settings)  # a rowless file gets its row first
    dest_dir = trash_dir(settings, brand)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_path(dest_dir, path.name)
    shutil.move(path, dest)
    os.utime(dest)  # the 30 days count from the delete, not from the photo's own mtime
    row = await session.get(StockImage, (brand.id, ref))
    assert row is not None
    row.deleted_at = datetime.now(UTC)
    await session.flush()
    return None


def purge_trash(settings: Settings, days: int = TRASH_DAYS) -> int:
    """Delete trash files older than ``days``. Returns how many were removed."""
    root = Path(settings.stock_images_dir) / ".trash"
    cutoff = time.time() - days * 86400
    n = 0
    for f in root.rglob("*") if root.is_dir() else []:
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            n += 1
    return n


def disk_usage_pct(settings: Settings) -> int | None:
    """Percent used of the volume holding the stock dir; None if it can't be read."""
    p = Path(settings.stock_images_dir).resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        u = shutil.disk_usage(p)
    except OSError:
        return None
    return round(100 * u.used / u.total)

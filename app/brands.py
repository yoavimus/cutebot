"""Brands (M10) — who the pipeline works for.

Every pipeline function takes a ``Brand`` (or its id); nothing reads the env chat id or
``brand.md`` to find out who it works for. ``brand.md`` is only the seed for row 1's text.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brand import _DEFAULT_BRAND, load_brand
from app.config import Settings
from app.models import Brand

logger = logging.getLogger(__name__)

DEFAULT_ID = 1


async def default(session: AsyncSession) -> Brand:
    """Brand 1. Raises if boot's ``ensure_default`` never ran (a broken deploy, not a state)."""
    brand = await session.get(Brand, DEFAULT_ID)
    if brand is None:
        raise RuntimeError("Default brand missing — ensure_default() did not run.")
    return brand


async def all_brands(session: AsyncSession) -> list[Brand]:
    return list(await session.scalars(select(Brand).order_by(Brand.id)))


async def by_chat_id(session: AsyncSession, chat_id: str | int | None) -> Brand | None:
    """The brand whose owner chat this is; None for any other chat (callers ignore it)."""
    if chat_id is None or str(chat_id) == "":
        return None
    return await session.scalar(select(Brand).where(Brand.telegram_chat_id == str(chat_id)))


async def ensure_default(session: AsyncSession, settings: Settings) -> Brand:
    """Create brand 1 from the env if missing; seed its text from ``BRAND_FILE`` when empty.

    The DB wins once seeded: editing ``brand.md`` later changes nothing. A blank chat id on
    the row is filled from ``TELEGRAM_CHAT_ID`` (the migration may have run without it).
    """
    brand = await session.get(Brand, DEFAULT_ID)
    if brand is None:
        brand = Brand(id=DEFAULT_ID, name="default")
        session.add(brand)
        logger.warning("Created default brand %s.", DEFAULT_ID)
    if not brand.telegram_chat_id and settings.telegram_chat_id:
        brand.telegram_chat_id = settings.telegram_chat_id
    if not brand.brand_text and Path(settings.brand_file).exists():
        brand.brand_text = load_brand(settings.brand_file)
        logger.warning("Seeded brand %s text from %s.", brand.id, settings.brand_file)
    await session.commit()
    return brand


def text(brand: Brand) -> str:
    """The brand guidelines the model sees — the single read of the brand text."""
    return brand.brand_text or _DEFAULT_BRAND

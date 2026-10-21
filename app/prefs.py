"""Runtime preferences — DB row wins, env (``Settings``) is the default (M9.2).

Per brand (M10): every function takes ``brand_id``. No cache: one indexed read per
command/tick. ``mode(session, brand_id)`` is the single read used by review, publish and
status. Reads raise on a DB error — boot decides how to fail closed.
"""

from __future__ import annotations

import logging
import re

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Setting

logger = logging.getLogger(__name__)

MODE = "mode"
GENERATION_CRON = "generation_cron"
POSTING_SLOTS = "posting_slots"

LIVE = "live"
LEARNING = "learning"

_SLOT = re.compile(r"^(\d{1,2}):(\d{2})$")
MAX_SLOTS = 6


async def get(
    session: AsyncSession, brand_id: int, key: str, default: str | None = None
) -> str | None:
    row = await session.get(Setting, (brand_id, key))
    return row.value if row is not None else default


async def set(session: AsyncSession, brand_id: int, key: str, value: str) -> None:  # noqa: A001
    row = await session.get(Setting, (brand_id, key))
    if row is None:
        session.add(Setting(brand_id=brand_id, key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def delete(session: AsyncSession, brand_id: int, key: str) -> None:
    row = await session.get(Setting, (brand_id, key))
    if row is not None:
        await session.delete(row)
        await session.commit()


async def mode(session: AsyncSession, brand_id: int) -> str:
    """``live`` (default) or ``learning``. Any unrecognised stored value fails closed."""
    value = await get(session, brand_id, MODE, LIVE)
    if value == LIVE:
        return LIVE
    if value != LEARNING:
        logger.error("Unrecognised stored mode %r — treating as learning.", value)
    return LEARNING


async def generation_cron(
    session: AsyncSession, brand_id: int, settings: Settings | None = None
) -> str:
    settings = settings or get_settings()
    stored = await get(session, brand_id, GENERATION_CRON)
    if stored is None:
        return settings.generation_cron
    try:
        CronTrigger.from_crontab(stored)
    except ValueError:
        logger.error("Stored generation_cron %r is invalid — using env default.", stored)
        return settings.generation_cron
    return stored


async def posting_slots(
    session: AsyncSession, brand_id: int, settings: Settings | None = None
) -> list[tuple[int, int]]:
    settings = settings or get_settings()
    stored = await get(session, brand_id, POSTING_SLOTS)
    if stored is None:
        return settings.posting_slots_list
    try:
        return parse_slots(stored)
    except ValueError:
        logger.error("Stored posting_slots %r is invalid — using env default.", stored)
        return settings.posting_slots_list


def parse_slots(raw: str) -> list[tuple[int, int]]:
    """Validate ``"12:00, 18:30"`` → sorted unique [(12, 0), (18, 30)]; ValueError otherwise."""
    slots: list[tuple[int, int]] = []
    for part in raw.replace(" ", "").split(","):
        m = _SLOT.match(part)
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            raise ValueError(f"bad slot {part!r} (want HH:MM)")
        slots.append((int(m[1]), int(m[2])))
    unique = sorted(dict.fromkeys(slots))  # not set(): this module defines ``set``
    if not 1 <= len(unique) <= MAX_SLOTS:
        raise ValueError(f"need 1–{MAX_SLOTS} slots")
    return unique


def format_slots(slots: list[tuple[int, int]]) -> str:
    return ",".join(f"{h:02d}:{m:02d}" for h, m in slots)


def parse_generation(raw: str) -> str:
    """``09:00`` → ``0 9 * * *``; otherwise a 5-field cron. ValueError when invalid."""
    raw = raw.strip().strip("\"'“”")
    m = _SLOT.match(raw)
    if m:
        if int(m[1]) > 23 or int(m[2]) > 59:
            raise ValueError(f"bad time {raw!r}")
        return f"{int(m[2])} {int(m[1])} * * *"
    CronTrigger.from_crontab(raw)  # raises ValueError on a bad expression
    return raw

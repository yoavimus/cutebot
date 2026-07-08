"""In-process APScheduler wiring — generation + posting-slot jobs. No Redis/worker (v1)."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.notifier.base import Notifier
from app.pipeline import generate, publish, review

logger = logging.getLogger(__name__)


def build_scheduler(
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    settings: Settings,
) -> AsyncIOScheduler:
    """Create a scheduler with the generation cron and one job per posting slot."""
    tz = settings.schedule_tz
    scheduler = AsyncIOScheduler(timezone=tz)

    async def generation_tick() -> None:
        async with sessionmaker() as session:
            posts = await generate.generate_batch(session)
            await review.send_for_review(posts, notifier)

    async def posting_tick() -> None:
        async with sessionmaker() as session:
            await publish.publish_next(session)

    scheduler.add_job(
        generation_tick,
        CronTrigger.from_crontab(settings.generation_cron, timezone=tz),
        id="generation",
        replace_existing=True,
        misfire_grace_time=300,
    )
    for hh, mm in settings.posting_slots_list:
        scheduler.add_job(
            posting_tick,
            CronTrigger(hour=hh, minute=mm, timezone=tz),
            id=f"posting-{hh:02d}{mm:02d}",
            replace_existing=True,
            misfire_grace_time=3600,
        )

    logger.info(
        "Scheduler built: generation=%r, posting_slots=%s",
        settings.generation_cron,
        settings.posting_slots_list,
    )
    return scheduler

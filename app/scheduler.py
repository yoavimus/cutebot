"""In-process APScheduler wiring — generation + posting-slot jobs. No Redis/worker (v1).

Cron and slots come from ``app.prefs`` (DB row, else env) so the owner can change them
from the bot; ``apply_slots`` / ``apply_mode`` / ``reschedule_generation`` mutate the
running scheduler in place — no restart.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import prefs, stock
from app.config import Settings
from app.notifier.base import Notifier
from app.pipeline import generate, publish, review

logger = logging.getLogger(__name__)

_POSTING_PREFIX = "posting-"


class CuteScheduler(AsyncIOScheduler):
    """AsyncIOScheduler that remembers the posting-slot job body so slots can be re-applied."""

    posting_tick: Callable[[], Awaitable[None]]


@dataclass
class Schedule:
    """What the scheduler should run at boot. ``degraded`` = settings were unreadable."""

    mode: str
    generation_cron: str
    slots: list[tuple[int, int]]
    degraded: bool = False


async def load_schedule(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> Schedule:
    """Read mode/cron/slots from prefs. **Fails closed**: any error → learning mode + env."""
    try:
        async with sessionmaker() as session:
            return Schedule(
                mode=await prefs.mode(session),
                generation_cron=await prefs.generation_cron(session, settings),
                slots=await prefs.posting_slots(session, settings),
            )
    except Exception:
        logger.exception("Could not read settings — starting in LEARNING mode (fail closed).")
        return Schedule(
            mode=prefs.LEARNING,
            generation_cron=settings.generation_cron,
            slots=settings.posting_slots_list,
            degraded=True,
        )


def apply_slots(
    scheduler: CuteScheduler,
    slots: list[tuple[int, int]],
    tz: str,
    *,
    paused: bool = False,
) -> None:
    """Make the ``posting-HHMM`` jobs exactly ``slots`` (add new, drop stale)."""
    wanted = {f"{_POSTING_PREFIX}{h:02d}{m:02d}": (h, m) for h, m in slots}
    for job in scheduler.get_jobs():
        if job.id.startswith(_POSTING_PREFIX) and job.id not in wanted:
            scheduler.remove_job(job.id)
    for job_id, (hh, mm) in wanted.items():
        extra = {"next_run_time": None} if paused else {}  # None = added paused
        scheduler.add_job(
            scheduler.posting_tick,
            CronTrigger(hour=hh, minute=mm, timezone=tz),
            id=job_id,
            replace_existing=True,
            misfire_grace_time=3600,
            **extra,
        )


def apply_mode(scheduler: CuteScheduler, mode: str) -> None:
    """learning → pause every posting-slot job (kept, not removed); live → resume them."""
    for job in scheduler.get_jobs():
        if job.id.startswith(_POSTING_PREFIX):
            if mode == prefs.LEARNING:
                job.pause()
            else:
                job.resume()


def reschedule_generation(scheduler: CuteScheduler, cron: str, tz: str) -> None:
    scheduler.reschedule_job("generation", trigger=CronTrigger.from_crontab(cron, timezone=tz))


def next_runs(scheduler: CuteScheduler) -> dict[str, datetime | None]:
    """job id → next fire time (None = paused)."""
    return {job.id: getattr(job, "next_run_time", None) for job in scheduler.get_jobs()}


def build_scheduler(
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    settings: Settings,
    schedule: Schedule | None = None,
) -> CuteScheduler:
    """Create a scheduler with the generation cron and one job per posting slot.

    ``schedule`` (from :func:`load_schedule`) overrides the env defaults; learning mode
    adds the slot jobs paused so nothing can fire between ``start()`` and the first read.
    """
    schedule = schedule or Schedule(
        mode=prefs.LIVE,
        generation_cron=settings.generation_cron,
        slots=settings.posting_slots_list,
    )
    tz = settings.schedule_tz
    scheduler = CuteScheduler(timezone=tz)

    async def generation_tick() -> None:
        async with sessionmaker() as session:
            posts = await generate.generate_batch(session)
            await review.send_for_review(posts, notifier)
            warning = await stock.low_stock_message(
                session, settings, len(posts), settings.batch_size
            )
            if warning:
                await notifier.send_message(warning)

    async def posting_tick() -> None:
        async with sessionmaker() as session:
            await publish.publish_next(session)

    scheduler.posting_tick = posting_tick
    scheduler.add_job(
        generation_tick,
        CronTrigger.from_crontab(schedule.generation_cron, timezone=tz),
        id="generation",
        replace_existing=True,
        misfire_grace_time=300,
    )
    apply_slots(scheduler, schedule.slots, tz, paused=schedule.mode == prefs.LEARNING)

    logger.info(
        "Scheduler built: mode=%s generation=%r, posting_slots=%s",
        schedule.mode,
        schedule.generation_cron,
        schedule.slots,
    )
    return scheduler

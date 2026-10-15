"""M9.2 — prefs fallback, /mode, /schedule, slot re-apply, fail-closed boot. Offline."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import prefs
from app import scheduler as sched
from app.config import Settings
from app.models import Feedback, Post, PostStatus, Setting
from app.notifier.telegram import process_callback, process_message
from app.pipeline import publish, review
from app.publishers.base import PublishResult

_OWNER = 42
_SETTINGS = Settings(
    telegram_bot_token="t",
    telegram_chat_id=str(_OWNER),
    posting_slots="12:00,18:00",
    generation_cron="0 9 * * *",
    schedule_tz="UTC",
    catchup_window_min=60,
)


class _Notifier:
    def __init__(self, scheduler: sched.CuteScheduler | None = None) -> None:
        self.scheduler = scheduler
        self.messages: list[str] = []
        self.toasts: list[str] = []

    async def send_suggestion(self, post: Post) -> None: ...
    async def send_message(self, text: str) -> None:
        self.messages.append(text)

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        self.toasts.append(text)

    async def mark_decided(self, cb_message: dict, post: Post, **kw: Any) -> None: ...


class _Ok:
    name = "ok"

    async def publish(self, session: AsyncSession, post: Post) -> PublishResult:
        return PublishResult(network="ok", ok=True, detail="")


def _scheduler() -> sched.CuteScheduler:
    async def noop() -> None: ...

    s = sched.CuteScheduler(timezone="UTC")
    s.posting_tick = noop
    return s


async def _cmd(session: AsyncSession, n: _Notifier, text: str) -> None:
    await process_message(session, n, {"chat": {"id": _OWNER}, "text": text}, _SETTINGS)


async def _post(session: AsyncSession, status: PostStatus = PostStatus.SUGGESTED) -> Post:
    p = Post(image_ref="a.jpg", caption_he="ה", caption_en="e", visual_concept="v", status=status)
    session.add(p)
    await session.commit()
    return p


def _posting_jobs(s: sched.CuteScheduler) -> dict[str, Any]:
    return {j.id: j for j in s.get_jobs() if j.id.startswith("posting-")}


# ─────────────────────────────── prefs ───────────────────────────────────────


async def test_prefs_fall_back_to_env_then_row_wins(session: AsyncSession) -> None:
    assert await prefs.mode(session) == "live"
    assert await prefs.generation_cron(session, _SETTINGS) == "0 9 * * *"
    assert await prefs.posting_slots(session, _SETTINGS) == [(12, 0), (18, 0)]
    await prefs.set(session, prefs.POSTING_SLOTS, "10:30")
    await prefs.set(session, prefs.GENERATION_CRON, "0 7 * * *")
    assert await prefs.posting_slots(session, _SETTINGS) == [(10, 30)]
    assert await prefs.generation_cron(session, _SETTINGS) == "0 7 * * *"
    await prefs.delete(session, prefs.POSTING_SLOTS)
    assert await prefs.posting_slots(session, _SETTINGS) == [(12, 0), (18, 0)]


async def test_garbage_mode_fails_closed(session: AsyncSession) -> None:
    await prefs.set(session, prefs.MODE, "banana")
    assert await prefs.mode(session) == "learning"


_SEVEN = "00:00,01:00,02:00,03:00,04:00,05:00,06:00"


@pytest.mark.parametrize("bad", ["", "25:00", "12:60", "noon", "1,2", _SEVEN])
def test_parse_slots_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        prefs.parse_slots(bad)


def test_parse_slots_sorts_and_dedupes() -> None:
    assert prefs.parse_slots("18:00, 9:05,18:00") == [(9, 5), (18, 0)]


def test_parse_generation() -> None:
    assert prefs.parse_generation("09:30") == "30 9 * * *"
    assert prefs.parse_generation('"0 9 * * 1-5"') == "0 9 * * 1-5"
    with pytest.raises(ValueError):
        prefs.parse_generation("every day")


# ─────────────────────────────── mode ────────────────────────────────────────


async def test_learning_mode_turns_approve_into_keep(session: AsyncSession) -> None:
    await prefs.set(session, prefs.MODE, "learning")
    p = await _post(session)
    out = await review.handle_decision(session, p.id, "approve")
    assert out is not None and out.status == PostStatus.KEPT and out.queue_position is None
    fb = (await session.scalars(select(Feedback))).all()
    assert [f.decision for f in fb] == ["approve"]  # still an approval signal for learning


async def test_learning_callback_toast_and_live_restores_approve(session: AsyncSession) -> None:
    await prefs.set(session, prefs.MODE, "learning")
    p = await _post(session)
    n = _Notifier()
    cb = {"id": "c", "data": f"approve:{p.id}", "message": {"message_id": 1, "chat": {"id": 1}}}
    await process_callback(session, n, cb, _SETTINGS)
    assert n.toasts == ["📚 Kept — learning mode"]
    await prefs.set(session, prefs.MODE, "live")
    p2 = await _post(session)
    out = await review.handle_decision(session, p2.id, "approve")
    assert out is not None and out.status == PostStatus.APPROVED


async def test_mode_command_pauses_and_resumes_slot_jobs(session: AsyncSession) -> None:
    s = _scheduler()
    sched.apply_slots(s, [(12, 0), (18, 0)], "UTC")
    s.start(paused=True)
    try:
        n = _Notifier(s)
        await _cmd(session, n, "/mode")
        assert n.messages[-1] == "Mode: live"
        await _cmd(session, n, "/mode learning")
        assert await prefs.mode(session) == "learning"
        assert all(j.next_run_time is None for j in _posting_jobs(s).values())
        await _cmd(session, n, "/mode live")
        assert all(j.next_run_time is not None for j in _posting_jobs(s).values())
        await _cmd(session, n, "/mode sideways")
        assert n.messages[-1].startswith("Usage")
        assert await prefs.mode(session) == "live"
    finally:
        s.shutdown(wait=False)


async def test_postnow_and_publish_refuse_in_learning_mode(session: AsyncSession) -> None:
    p = await _post(session)
    await review.handle_decision(session, p.id, "approve")  # approved while live
    await prefs.set(session, prefs.MODE, "learning")
    n = _Notifier()
    await _cmd(session, n, "/postnow")
    await _cmd(session, n, f"/postnow {p.id}")
    assert all("Learning mode" in m for m in n.messages) and len(n.messages) == 2
    assert await publish.publish_next(session, [_Ok()]) is None  # slot tick / catch-up door
    await session.refresh(p)
    assert p.status == PostStatus.APPROVED


async def test_status_header_in_learning_mode(session: AsyncSession) -> None:
    await prefs.set(session, prefs.MODE, "learning")
    n = _Notifier()
    await _cmd(session, n, "/status")
    assert n.messages[0].startswith("🎓 LEARNING MODE — nothing publishes")


# ─────────────────────────────── schedule ────────────────────────────────────


async def test_apply_slots_replaces_job_ids() -> None:
    s = _scheduler()
    sched.apply_slots(s, [(12, 0), (18, 0)], "UTC")
    assert set(_posting_jobs(s)) == {"posting-1200", "posting-1800"}
    sched.apply_slots(s, [(10, 0), (18, 0)], "UTC")
    assert set(_posting_jobs(s)) == {"posting-1000", "posting-1800"}


async def test_schedule_post_generate_reset(session: AsyncSession) -> None:
    s = sched.build_scheduler(
        async_sessionmaker(), _Notifier(), _SETTINGS  # type: ignore[arg-type]
    )
    s.start(paused=True)
    try:
        n = _Notifier(s)
        await _cmd(session, n, "/schedule post 10:00")
        assert set(_posting_jobs(s)) == {"posting-1000"}
        assert await prefs.get(session, prefs.POSTING_SLOTS) == "10:00"

        await _cmd(session, n, "/schedule generate 07:15")
        assert str(s.get_job("generation").trigger).startswith("cron[")
        assert await prefs.get(session, prefs.GENERATION_CRON) == "15 7 * * *"

        before = await prefs.get(session, prefs.POSTING_SLOTS)
        await _cmd(session, n, "/schedule post 99:00")
        assert n.messages[-1].count("Usage") == 1
        assert await prefs.get(session, prefs.POSTING_SLOTS) == before  # nothing stored

        await _cmd(session, n, "/schedule reset")
        assert set(_posting_jobs(s)) == {"posting-1200", "posting-1800"}
        assert await session.scalar(select(Setting).where(Setting.key != "mode")) is None
    finally:
        s.shutdown(wait=False)


async def test_schedule_post_in_learning_mode_stays_paused(session: AsyncSession) -> None:
    await prefs.set(session, prefs.MODE, "learning")
    s = _scheduler()
    s.start(paused=True)
    try:
        await _cmd(session, _Notifier(s), "/schedule post 11:00")
        assert all(j.next_run_time is None for j in _posting_jobs(s).values())
    finally:
        s.shutdown(wait=False)


async def test_catch_up_uses_stored_slots(session: AsyncSession) -> None:
    p = await _post(session)
    await review.handle_decision(session, p.id, "approve")
    await prefs.set(session, prefs.POSTING_SLOTS, "09:00")  # env says 12:00/18:00
    now = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)
    out = await publish.catch_up_missed_slot(session, _SETTINGS, now=now, publishers=[_Ok()])
    assert out is not None and out.status == PostStatus.PUBLISHED


# ─────────────────────────────── boot ────────────────────────────────────────


async def test_load_schedule_applies_stored_mode(session: AsyncSession) -> None:
    await prefs.set(session, prefs.MODE, "learning")
    sm = async_sessionmaker(session.bind, expire_on_commit=False)
    loaded = await sched.load_schedule(sm, _SETTINGS)
    assert loaded.mode == "learning" and not loaded.degraded
    s = sched.build_scheduler(sm, _Notifier(), _SETTINGS, loaded)
    s.start(paused=True)
    try:
        assert all(j.next_run_time is None for j in _posting_jobs(s).values())
        assert len(_posting_jobs(s)) == 2
    finally:
        s.shutdown(wait=False)


async def test_unreadable_settings_table_boots_in_learning_mode() -> None:
    from app.db import make_engine, make_sessionmaker

    engine = make_engine("sqlite+aiosqlite:///:memory:")  # no tables at all
    try:
        loaded = await sched.load_schedule(make_sessionmaker(engine), _SETTINGS)
    finally:
        await engine.dispose()
    assert loaded.mode == "learning" and loaded.degraded
    assert loaded.slots == [(12, 0), (18, 0)]


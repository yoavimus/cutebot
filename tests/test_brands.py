"""M10.1 — brand row, chat → brand resolution, per-brand filtering, dry-run cycle. Offline."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import brands, learning, llm, prefs, stock
from app import scheduler as sched
from app.brand import _DEFAULT_BRAND
from app.config import Settings
from app.models import Batch, Brand, Feedback, Post, PostStatus
from app.notifier.telegram import TelegramNotifier, process_callback, process_message
from app.pipeline import generate, publish, queue, review
from app.schemas import PostSuggestion

_S = Settings(telegram_chat_id="42", stock_images_dir="stock")


class _Notifier:
    scheduler = None

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.toasts: list[str] = []
        self.suggestions: list[Post] = []
        self.addressed: list[int] = []

    def for_brand(self, brand: Brand) -> _Notifier:
        self.addressed.append(brand.id)
        return self

    async def send_suggestion(self, post: Post) -> None:
        self.suggestions.append(post)

    async def send_message(self, text: str, *, html: bool = False, keyboard: object = None) -> None:
        self.messages.append(text)

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        self.toasts.append(text)

    async def mark_decided(self, cb_message: dict, post: Post, **kw: object) -> None: ...


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def caption(brand: str, image: Path, s: Settings, ctx: object = None) -> PostSuggestion:
        return PostSuggestion(caption_he="ה", caption_en="e", visual_concept="v", rationale="r")

    async def select_images(
        session: AsyncSession, brand: Brand, n: int, settings: Settings
    ) -> list[Path]:
        return [Path(f"{brand.stock_prefix or 'top'}-{i}.jpg") for i in range(n)]

    monkeypatch.setattr(llm, "caption_image", caption)
    monkeypatch.setattr(stock, "select_images", select_images)


async def _second(session: AsyncSession) -> Brand:
    b = Brand(id=2, name="other", telegram_chat_id="77", brand_text="other brand", stock_prefix="o")
    session.add(b)
    await session.commit()
    return b


def _post(brand_id: int, status: PostStatus = PostStatus.SUGGESTED, **kw: object) -> Post:
    return Post(
        brand_id=brand_id,
        image_ref="a.jpg",
        caption_he=f"brand {brand_id}",
        caption_en="e",
        visual_concept="v",
        status=status,
        **kw,
    )


def _cb(data: str, chat: int) -> dict:
    return {"id": "c", "data": data, "message": {"message_id": 1, "chat": {"id": chat}}}


# ─────────────────────────────── row + seed ──────────────────────────────────


async def test_ensure_default_creates_and_seeds_once(tmp_path: Path) -> None:
    from app.db import Base, make_engine, make_sessionmaker

    brand_md = tmp_path / "brand.md"
    brand_md.write_text("# Seeded brand\n", encoding="utf-8")
    s = Settings(telegram_chat_id="42", brand_file=str(brand_md))
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with make_sessionmaker(engine)() as session:
        b = await brands.ensure_default(session, s)
        assert (b.id, b.name, b.telegram_chat_id) == (1, "default", "42")
        assert b.brand_text == "# Seeded brand\n" and brands.text(b) == b.brand_text
        brand_md.write_text("edited on the volume\n", encoding="utf-8")
        again = await brands.ensure_default(session, s)
        assert again.brand_text == "# Seeded brand\n"  # the DB wins once seeded
    await engine.dispose()


async def test_missing_brand_file_falls_back_and_blank_chat_is_filled(
    session: AsyncSession, tmp_path: Path
) -> None:
    b = await brands.default(session)
    b.brand_text, b.telegram_chat_id = "", None
    await session.commit()
    s = Settings(telegram_chat_id="42", brand_file=str(tmp_path / "missing.md"))
    b = await brands.ensure_default(session, s)
    assert b.brand_text == "" and brands.text(b) == _DEFAULT_BRAND
    assert b.telegram_chat_id == "42"


async def test_by_chat_id(session: AsyncSession) -> None:
    await _second(session)
    assert (await brands.by_chat_id(session, 42)).id == 1  # type: ignore[union-attr]
    assert (await brands.by_chat_id(session, "77")).id == 2  # type: ignore[union-attr]
    assert await brands.by_chat_id(session, 999) is None
    assert await brands.by_chat_id(session, "") is None
    assert await brands.by_chat_id(session, None) is None


def test_for_brand_addresses_the_brand_chat() -> None:
    env = TelegramNotifier(Settings(telegram_chat_id="42"))
    assert env._chat_id == "42"  # process-wide: the env chat, for boot warnings only
    assert env.for_brand(Brand(id=2, name="x", telegram_chat_id="77"))._chat_id == "77"
    # a brand without a chat never falls back to someone else's chat
    assert env.for_brand(Brand(id=3, name="y", telegram_chat_id=None))._chat_id == ""


# ─────────────────────────────── chat resolution ─────────────────────────────


async def test_unknown_chat_is_ignored(session: AsyncSession) -> None:
    p = _post(1)
    session.add(p)
    await session.commit()
    n = _Notifier()
    await process_message(session, n, {"chat": {"id": 999}, "text": "/status"}, _S)
    await process_callback(session, n, _cb(f"approve:{p.id}", 999), _S)
    assert n.messages == [] and n.toasts == [] and n.addressed == []
    await session.refresh(p)
    assert p.status == PostStatus.SUGGESTED


async def test_chat_resolves_to_its_brand(session: AsyncSession) -> None:
    await _second(session)
    n = _Notifier()
    await process_message(session, n, {"chat": {"id": 77}, "text": "/generate 1"}, _S)
    assert n.addressed == [2]
    assert [(p.brand_id, p.image_ref) for p in n.suggestions] == [(2, "o-0.jpg")]


async def test_other_brands_post_is_not_found(session: AsyncSession) -> None:
    await _second(session)
    theirs = _post(2)
    session.add(theirs)
    await session.commit()
    n = _Notifier()
    await process_callback(session, n, _cb(f"approve:{theirs.id}", 42), _S)
    assert n.toasts == ["Post not found"]
    await process_callback(session, n, _cb(f"reject:{theirs.id}", 42), _S)
    assert n.toasts[-1] == "Post not found"
    await session.refresh(theirs)
    assert theirs.status == PostStatus.SUGGESTED
    assert (await session.scalars(select(Feedback))).all() == []
    assert await publish.publish_by_id(session, 1, theirs.id) is None


# ─────────────────────────────── per-brand filtering ─────────────────────────


async def test_queue_prefs_learning_and_bans_are_per_brand(session: AsyncSession) -> None:
    await _second(session)
    mine, theirs = _post(1), _post(2)
    session.add_all([mine, theirs])
    await session.commit()
    await review.handle_decision(session, 2, theirs.id, "approve")
    await review.handle_decision(session, 1, mine.id, "approve")
    assert (mine.queue_position, theirs.queue_position) == (1, 1)  # separate queues
    assert (await queue.peek_next(session, 1)).id == mine.id  # type: ignore[union-attr]
    assert await queue.queue_length(session, 2) == 1

    await prefs.set(session, 2, prefs.MODE, prefs.LEARNING)
    assert await prefs.mode(session, 1) == prefs.LIVE
    assert await publish.publish_next(session, 2) is None  # brand 2 learns, brand 1 unaffected

    ctx = await learning.build_context(session, 1, _S)
    assert [e.caption_he for e in ctx.examples] == ["brand 1"]
    n = await learning.verdict_counts(session, 2, _S)
    assert sum(n.values()) == 1

    await stock.ban_image(session, 2, "a.jpg")
    await session.commit()
    assert not await stock.unban_image(session, 1, "a.jpg")
    assert await stock.unban_image(session, 2, "a.jpg")


async def test_status_counts_only_the_chats_brand(session: AsyncSession) -> None:
    await _second(session)
    session.add_all([_post(1), _post(2), _post(2)])
    await session.commit()
    n = _Notifier()
    await process_message(session, n, {"chat": {"id": 77}, "text": "/status"}, _S)
    assert "suggested  2" in n.messages[0]


# ─────────────────────────────── dry-run cycle ───────────────────────────────


async def test_dry_run_cycle_writes_brand_id_everywhere(session: AsyncSession) -> None:
    b = await brands.default(session)
    posts = await generate.generate_batch(session, b, n=2)
    for p in posts:
        await review.handle_decision(session, b.id, p.id, "approve")
    rows = [
        *(await session.scalars(select(Batch))).all(),
        *(await session.scalars(select(Post))).all(),
        *(await session.scalars(select(Feedback))).all(),
    ]
    assert len(rows) == 5 and {r.brand_id for r in rows} == {1}
    batch = (await session.scalars(select(Batch))).one()
    assert batch.brand_snapshot == "test brand"  # the row's text, not brand.md


async def test_generation_tick_runs_every_brand(session: AsyncSession) -> None:
    await _second(session)
    sm = async_sessionmaker(session.bind, expire_on_commit=False)
    n = _Notifier()
    s = sched.build_scheduler(sm, n, _S)
    tick = s.get_job("generation").func  # type: ignore[union-attr]
    await tick()
    assert n.addressed == [1, 2]
    assert {p.brand_id for p in n.suggestions} == {1, 2}

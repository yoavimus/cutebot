"""M8.1 — learning context selection + prompt rendering (offline, in-memory DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.learning import LearningContext, build_context, render
from app.llm import _user_prompt
from app.models import Decision, Feedback, Post, PostStatus

_S = Settings(learning_examples=3, learning_recent=2, learning_window_days=30)


def _post(i: int, status: PostStatus) -> Post:
    now = datetime.now(UTC)
    return Post(
        id=i,
        image_ref=f"{i}.jpg",
        caption_he=f"he{i}",
        caption_en=f"en{i}",
        visual_concept="v",
        status=status,
        decided_at=now + timedelta(minutes=i),
        published_at=now + timedelta(minutes=i) if status == PostStatus.PUBLISHED else None,
    )


async def test_cold_start_is_empty_and_renders_nothing(session: AsyncSession) -> None:
    ctx = await build_context(session, _S)
    assert ctx.is_empty
    assert render(ctx) == ""
    assert "approved_examples" not in _user_prompt("brand", ctx)
    assert _user_prompt("brand", ctx) == _user_prompt("brand", None)


async def test_examples_edited_first_then_newest_and_capped(session: AsyncSession) -> None:
    session.add_all(_post(i, PostStatus.APPROVED) for i in range(1, 6))
    session.add(_post(6, PostStatus.REJECTED))  # never an example
    session.add(
        Feedback(
            post_id=1,
            decision=Decision.APPROVE,
            edit_lang="he",
            edit_before="old",
            edit_after="he1",
        )
    )
    await session.commit()
    ctx = await build_context(session, _S)
    assert [e.caption_he for e in ctx.examples] == ["he1", "he5", "he4"]
    assert [e.edited for e in ctx.examples] == [True, False, False]
    assert [(e.before, e.after) for e in ctx.edits] == [("old", "he1")]
    assert "corrected this one by hand" in render(ctx)


async def test_hints_only_for_in_window_known_reasons(session: AsyncSession) -> None:
    session.add(_post(1, PostStatus.REJECTED))
    old = datetime.now(UTC) - timedelta(days=60)
    session.add_all(
        [
            Feedback(post_id=1, decision=Decision.REJECT, reason="hebrew"),
            Feedback(post_id=1, decision=Decision.REJECT, reason="hebrew"),
            Feedback(post_id=1, decision=Decision.REJECT, reason="hebrew"),
            Feedback(post_id=1, decision=Decision.REJECT, reason="boring", created_at=old),
            Feedback(post_id=1, decision=Decision.REJECT, reason="image"),
            Feedback(post_id=1, decision=Decision.REJECT, reason="voice"),
            Feedback(post_id=1, decision=Decision.APPROVE, reason="voice"),  # not a reject
        ]
    )
    await session.commit()
    ctx = await build_context(session, _S)
    assert ctx.reject_hints == {"hebrew": 3, "voice": 1}
    out = render(ctx)
    assert "cited repeatedly unnatural Hebrew" in out
    assert "cited off-brand voice" in out  # once → no "repeatedly"
    assert "boring" not in out and "image" not in out


async def test_recent_is_published_only_newest_capped(session: AsyncSession) -> None:
    session.add_all(_post(i, PostStatus.PUBLISHED) for i in range(1, 5))
    session.add(_post(5, PostStatus.APPROVED))
    await session.commit()
    ctx = await build_context(session, _S)
    assert ctx.recent == ["he4", "he3"]
    assert "<recent_posts>" in render(ctx)


async def test_disabled_gives_empty_context(session: AsyncSession) -> None:
    session.add(_post(1, PostStatus.PUBLISHED))
    await session.commit()
    ctx = await build_context(session, _S.model_copy(update={"learning_enabled": False}))
    assert ctx == LearningContext()


def test_blocks_appear_only_when_populated() -> None:
    assert "<recent_posts>" not in render(LearningContext(reject_hints={"boring": 1}))
    assert "<approved_examples>" not in render(LearningContext(recent=["x"]))

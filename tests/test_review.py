"""M9.1 — verdicts (Keep / Almost / Retry) in ``review`` (offline, LLM + stock stubbed)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import brands, llm, stock
from app.config import Settings
from app.learning import LearningContext
from app.models import Brand, Decision, Feedback, Post, PostStatus
from app.pipeline import generate, queue, review
from app.schemas import PostSuggestion

_S = Settings()
_contexts: list[LearningContext | None] = []


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    _contexts.clear()

    async def fake_caption(
        brand: str, image_path: Path, settings: Settings, context: LearningContext | None = None
    ) -> PostSuggestion:
        _contexts.append(context)
        return PostSuggestion(
            caption_he=f"he{len(_contexts)}", caption_en="en", visual_concept="v", rationale="r"
        )

    async def fake_select(
        session: AsyncSession, brand: Brand, n: int, settings: Settings
    ) -> list[Path]:
        return [Path(f"{i}.jpg") for i in range(n)]

    monkeypatch.setattr(llm, "caption_image", fake_caption)
    monkeypatch.setattr(stock, "select_images", fake_select)


async def _one(session: AsyncSession) -> Post:
    return (await generate.generate_batch(session, await brands.default(session), n=1))[0]


async def _decisions(session: AsyncSession, post_id: int) -> list[str]:
    rows = await session.scalars(
        select(Feedback.decision).where(Feedback.post_id == post_id).order_by(Feedback.id)
    )
    return list(rows)


async def test_keep_is_an_approval_that_never_queues(session: AsyncSession) -> None:
    post = await _one(session)
    kept = await review.handle_decision(session, 1, post.id, "keep")
    assert kept is not None and kept.status == PostStatus.KEPT
    assert kept.queue_position is None
    assert await queue.peek_next(session, 1) is None
    assert await review.handle_decision(session, 1, post.id, "keep") is not None  # no-op repeat
    assert await _decisions(session, post.id) == [Decision.APPROVE]


async def test_keep_approve_reject_reversals(session: AsyncSession) -> None:
    post = await _one(session)
    await review.handle_decision(session, 1, post.id, "keep")
    promoted = await review.handle_decision(session, 1, post.id, "approve")  # 📤 Publish after all
    assert promoted is not None and promoted.status == PostStatus.APPROVED
    assert (await queue.peek_next(session, 1)) is promoted
    back = await review.handle_decision(session, 1, post.id, "keep")  # 📚 Keep instead
    assert back is not None and back.status == PostStatus.KEPT and back.queue_position is None
    rejected = await review.handle_decision(session, 1, post.id, "reject")
    assert rejected is not None and rejected.status == PostStatus.REJECTED
    assert len(await _decisions(session, post.id)) == 4


async def test_almost_rejects_with_reason(session: AsyncSession) -> None:
    post = await _one(session)
    out = await review.handle_decision(session, 1, post.id, "almost", reason="hebrew")
    assert out is not None and out.status == PostStatus.REJECTED
    fb = await session.scalar(select(Feedback).where(Feedback.post_id == post.id))
    assert fb is not None and (fb.decision, fb.reason) == (Decision.ALMOST, "hebrew")


@pytest.mark.parametrize("verdict", ["keep", "almost"])
async def test_terminal_untouched_by_new_verdicts(session: AsyncSession, verdict: str) -> None:
    post = await _one(session)
    post.status = PostStatus.PUBLISHED
    await session.commit()
    out = await review.handle_decision(session, 1, post.id, verdict)
    assert out is not None and out.status == PostStatus.PUBLISHED
    assert await _decisions(session, post.id) == []


async def test_retry_recaptions_same_image_once(session: AsyncSession) -> None:
    post = await _one(session)
    await review.handle_decision(session, 1, post.id, "almost", reason="boring")
    new = await review.retry(session, await brands.default(session), post.id, _S)
    assert new is not None and new.id != post.id
    assert (new.status, new.image_ref, new.retry_of, new.batch_id) == (
        PostStatus.SUGGESTED,
        post.image_ref,
        post.id,
        post.batch_id,
    )
    ctx = _contexts[-1]
    assert ctx is not None and ctx.retry_of is not None
    assert (ctx.retry_of.caption_he, ctx.retry_of.reason) == (post.caption_he, "boring")
    # a second tap returns the same retry, no new model call
    calls = len(_contexts)
    again = await review.retry(session, await brands.default(session), post.id, _S)
    assert again is not None and again.id == new.id and len(_contexts) == calls


async def test_retry_chain_and_refusals(session: AsyncSession) -> None:
    post = await _one(session)
    assert (
        await review.retry(session, await brands.default(session), post.id, _S) is None
    )  # suggested
    await review.handle_decision(session, 1, post.id, "reject")
    assert (
        await review.retry(session, await brands.default(session), post.id, _S) is None
    )  # plain ❌, not 🤏
    await review.handle_decision(session, 1, post.id, "approve")
    await review.handle_decision(session, 1, post.id, "almost")
    child = await review.retry(session, await brands.default(session), post.id, _S)
    assert child is not None
    await review.handle_decision(session, 1, child.id, "almost")
    grandchild = await review.retry(session, await brands.default(session), child.id, _S)
    assert grandchild is not None and grandchild.retry_of == child.id

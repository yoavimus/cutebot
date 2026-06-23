"""End-to-end pipeline test: generate → approve/reject → queue → publish.

The LLM, stock library, and publishers are stubbed; no network or real files touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import llm, stock
from app.config import Settings, get_settings
from app.models import Decision, Feedback, Post, PostStatus
from app.pipeline import generate, publish, queue, review
from app.publishers.base import Publisher, PublishResult
from app.render import render_full_caption
from app.schemas import PostSuggestion


class _RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_suggestion(self, post: Post) -> None:
        self.sent.append(post.id)


class _OkPublisher:
    name = "test"

    async def publish(self, post: Post) -> PublishResult:
        return PublishResult(network=self.name, ok=True)


class _FailPublisher:
    name = "broken"

    async def publish(self, post: Post) -> PublishResult:
        return PublishResult(network=self.name, ok=False, detail="boom")


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_caption_image(
        brand: str, image_path: Path, settings: Settings
    ) -> PostSuggestion:
        return PostSuggestion(
            caption_he=f"כיתוב {image_path.name}",
            caption_en=f"caption {image_path.name}",
            visual_concept=f"vis {image_path.name}",
            rationale="r",
        )

    monkeypatch.setattr(llm, "caption_image", fake_caption_image)


@pytest.fixture(autouse=True)
def _stub_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_select_images(session: AsyncSession, n: int, settings: Settings) -> list[Path]:
        return [Path(f"{chr(ord('a') + i)}.jpg") for i in range(n)]

    monkeypatch.setattr(stock, "select_images", fake_select_images)


async def test_generate_creates_suggested_posts(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=3, brand="brand text")
    assert len(posts) == 3
    assert all(p.status == PostStatus.SUGGESTED for p in posts)
    assert all(p.batch_id is not None for p in posts)
    assert all(p.image_ref for p in posts)
    assert all(p.caption_he for p in posts)
    assert all(p.caption_en for p in posts)


async def test_render_full_caption_has_both_languages_and_disclaimer(
    session: AsyncSession,
) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    settings = get_settings()
    rendered = render_full_caption(posts[0], settings)
    assert posts[0].caption_he in rendered
    assert posts[0].caption_en in rendered
    assert settings.post_disclaimer in rendered
    assert rendered.index(posts[0].caption_he) < rendered.index(posts[0].caption_en)


async def test_send_for_review_notifies_each(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=2, brand="b")
    notifier = _RecordingNotifier()
    await review.send_for_review(posts, notifier)
    assert notifier.sent == [p.id for p in posts]


async def test_approve_enqueues_and_writes_feedback(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=2, brand="b")
    approved = await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    assert approved is not None
    assert approved.status == PostStatus.APPROVED
    assert approved.queue_position == 1

    fb = (await session.scalars(select(Feedback).where(Feedback.post_id == posts[0].id))).all()
    assert len(fb) == 1 and fb[0].decision == Decision.APPROVE


async def test_reject_sets_status_and_skips_queue(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    rejected = await review.handle_decision(session, posts[0].id, Decision.REJECT)
    assert rejected is not None and rejected.status == PostStatus.REJECTED
    assert await queue.queue_length(session) == 0


async def test_decision_is_idempotent(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    # Second decision must not double-write feedback or change state.
    again = await review.handle_decision(session, posts[0].id, Decision.REJECT)
    assert again is not None and again.status == PostStatus.APPROVED
    fb = (await session.scalars(select(Feedback))).all()
    assert len(fb) == 1


async def test_publish_drains_front_of_queue(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=2, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    await review.handle_decision(session, posts[1].id, Decision.APPROVE)

    pubs: list[Publisher] = [_OkPublisher()]
    first = await publish.publish_next(session, pubs)
    assert first is not None and first.id == posts[0].id
    assert first.status == PostStatus.PUBLISHED
    assert first.published_at is not None
    assert await queue.queue_length(session) == 1


async def test_publish_empty_queue_returns_none(session: AsyncSession) -> None:
    assert await publish.publish_next(session, [_OkPublisher()]) is None


async def test_publish_marks_failed_when_a_network_fails(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    result = await publish.publish_next(session, [_OkPublisher(), _FailPublisher()])
    assert result is not None and result.status == PostStatus.FAILED

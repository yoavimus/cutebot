"""End-to-end pipeline test: generate → approve/reject → queue → publish.

The LLM, stock library, and publishers are stubbed; no network or real files touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import llm, stock
from app.config import Settings, get_settings
from app.llm import CaptionError
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


async def test_same_decision_is_idempotent(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    # Re-sending the same decision must not write a second Feedback row.
    again = await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    assert again is not None and again.status == PostStatus.APPROVED
    fb = (await session.scalars(select(Feedback))).all()
    assert len(fb) == 1


async def test_flip_approve_to_reject(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    post = await review.handle_decision(session, posts[0].id, Decision.REJECT)
    assert post is not None and post.status == PostStatus.REJECTED
    assert post.queue_position is None
    fb = (await session.scalars(select(Feedback).where(Feedback.post_id == posts[0].id))).all()
    assert len(fb) == 2


async def test_flip_reject_to_approve(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.REJECT)
    post = await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    assert post is not None and post.status == PostStatus.APPROVED
    assert post.queue_position is not None
    fb = (await session.scalars(select(Feedback).where(Feedback.post_id == posts[0].id))).all()
    assert len(fb) == 2


async def test_terminal_posts_cannot_be_flipped(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    await session.refresh(posts[0])
    posts[0].status = PostStatus.PUBLISHED
    await session.commit()
    post = await review.handle_decision(session, posts[0].id, Decision.REJECT)
    assert post is not None and post.status == PostStatus.PUBLISHED
    fb = (await session.scalars(select(Feedback))).all()
    assert len(fb) == 1  # original approve only


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


async def test_generate_skips_failed_image_keeps_good_ones(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_count = 0

    async def flaky_caption(brand: str, image_path: Path, settings: Settings) -> PostSuggestion:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise CaptionError("simulated API failure")
        return PostSuggestion(
            caption_he=f"כיתוב {image_path.name}",
            caption_en=f"caption {image_path.name}",
            visual_concept="vis",
            rationale="r",
        )

    monkeypatch.setattr(llm, "caption_image", flaky_caption)
    posts = await generate.generate_batch(session, n=3, brand="b")
    assert len(posts) == 2
    assert all(p.status == PostStatus.SUGGESTED for p in posts)


async def test_generate_all_failed_returns_empty_batch(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def always_fail(brand: str, image_path: Path, settings: Settings) -> PostSuggestion:
        raise CaptionError("simulated total failure")

    monkeypatch.setattr(llm, "caption_image", always_fail)
    posts = await generate.generate_batch(session, n=2, brand="b")
    assert posts == []


# ---------------------------------------------------------------------------
# M3 — publish path: ordering, idempotency, crash recovery, requeue


async def test_ordering_three_posts(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=3, brand="b")
    for p in posts:
        await review.handle_decision(session, p.id, Decision.APPROVE)

    pubs: list[Publisher] = [_OkPublisher()]
    results = [await publish.publish_next(session, pubs) for _ in range(3)]
    assert [r.id for r in results] == [p.id for p in posts]
    assert all(r.status == PostStatus.PUBLISHED for r in results)
    assert await queue.queue_length(session) == 0
    assert await publish.publish_next(session, pubs) is None


async def test_publishing_post_not_re_peeked(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=2, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    await review.handle_decision(session, posts[1].id, Decision.APPROVE)
    # Simulate crash: posts[0] claimed as PUBLISHING but never completed
    await session.refresh(posts[0])
    posts[0].status = PostStatus.PUBLISHING
    await session.commit()
    # publish_next must pick posts[1], not the stuck PUBLISHING post
    result = await publish.publish_next(session, [_OkPublisher()])
    assert result is not None and result.id == posts[1].id


async def test_recover_orphaned(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    await session.refresh(posts[0])
    original_pos = posts[0].queue_position
    # Simulate crash: post stuck in PUBLISHING
    posts[0].status = PostStatus.PUBLISHING
    await session.commit()
    # Sweep: should reset to APPROVED with queue_position intact
    n = await publish.recover_orphaned(session)
    assert n == 1
    await session.refresh(posts[0])
    assert posts[0].status == PostStatus.APPROVED
    assert posts[0].queue_position == original_pos
    # Next slot publishes it exactly once
    result = await publish.publish_next(session, [_OkPublisher()])
    assert result is not None and result.id == posts[0].id
    assert result.status == PostStatus.PUBLISHED


async def test_failed_post_requeue(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.APPROVE)
    failed = await publish.publish_next(session, [_FailPublisher()])
    assert failed is not None and failed.status == PostStatus.FAILED
    assert await queue.queue_length(session) == 0
    # Requeue puts it back
    await queue.requeue(session, failed)
    await session.commit()
    assert failed.status == PostStatus.APPROVED
    published = await publish.publish_next(session, [_OkPublisher()])
    assert published is not None and published.id == failed.id
    assert published.status == PostStatus.PUBLISHED


async def test_full_state_transition_arc(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    post = posts[0]
    assert post.status == PostStatus.SUGGESTED
    assert post.published_at is None

    await review.handle_decision(session, post.id, Decision.APPROVE)
    await session.refresh(post)
    assert post.status == PostStatus.APPROVED

    result = await publish.publish_next(session, [_OkPublisher()])
    assert result is not None and result.status == PostStatus.PUBLISHED
    assert result.published_at is not None


# ───────────────────────────── startup slot catch-up ─────────────────────────


def _catchup_settings() -> Settings:
    return Settings(
        posting_slots="12:00,18:00", schedule_tz="UTC", catchup_window_min=60
    )


async def test_catch_up_publishes_when_slot_missed(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.APPROVE)

    # Fixed past date: 30 min after the 12:00 slot, inside the 60-min window.
    now = datetime(2026, 1, 5, 12, 30, tzinfo=UTC)
    result = await publish.catch_up_missed_slot(
        session, _catchup_settings(), now=now, publishers=[_OkPublisher()]
    )
    assert result is not None and result.status == PostStatus.PUBLISHED

    # Second startup with something already published since the slot → no double-post.
    again = await publish.catch_up_missed_slot(
        session, _catchup_settings(), now=now, publishers=[_OkPublisher()]
    )
    assert again is None


async def test_catch_up_skips_outside_window(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await review.handle_decision(session, posts[0].id, Decision.APPROVE)

    now = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)  # 2.5h after the 12:00 slot
    result = await publish.catch_up_missed_slot(
        session, _catchup_settings(), now=now, publishers=[_OkPublisher()]
    )
    assert result is None
    assert await queue.queue_length(session) == 1  # untouched


async def test_catch_up_noop_without_slots(session: AsyncSession) -> None:
    settings = Settings(posting_slots="", schedule_tz="UTC")
    assert await publish.catch_up_missed_slot(session, settings) is None

"""Offline tests for process_callback and mark_decided."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app import llm, stock
from app.config import Settings
from app.models import Post, PostStatus
from app.notifier.telegram import process_callback, process_message
from app.pipeline import generate
from app.schemas import PostSuggestion


class _FakeNotifier:
    def __init__(self) -> None:
        self.marks: list[tuple[dict, Post]] = []
        self.toasts: list[tuple[str, str]] = []
        self.messages: list[str] = []

    async def mark_decided(self, cb_message: dict, post: Post) -> None:
        self.marks.append((cb_message, post))

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        self.toasts.append((callback_query_id, text))

    async def send_message(self, text: str) -> None:
        self.messages.append(text)


def _photo_cb(post_id: int, decision: str) -> dict:
    return {
        "id": "cq1",
        "data": f"{decision}:{post_id}",
        "message": {
            "message_id": 10,
            "chat": {"id": 99},
            "photo": [{"file_id": "x"}],
            "caption": "original caption",
        },
    }


def _text_cb(post_id: int, decision: str) -> dict:
    return {
        "id": "cq2",
        "data": f"{decision}:{post_id}",
        "message": {
            "message_id": 11,
            "chat": {"id": 99},
            "text": "original text",
        },
    }


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_caption(brand: str, image_path: Path, settings: Settings) -> PostSuggestion:
        return PostSuggestion(
            caption_he="כיתוב",
            caption_en="caption",
            visual_concept="vis",
            rationale="r",
        )

    monkeypatch.setattr(llm, "caption_image", fake_caption)


@pytest.fixture(autouse=True)
def _stub_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_select(session: AsyncSession, n: int, settings: Settings) -> list[Path]:
        return [Path(f"{i}.jpg") for i in range(n)]

    monkeypatch.setattr(stock, "select_images", fake_select)


async def test_fresh_approve(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "approve"))
    assert notifier.toasts[0][1] == "✅ Approved"
    assert len(notifier.marks) == 1
    assert notifier.marks[0][1].status == PostStatus.APPROVED


async def test_fresh_reject(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _text_cb(posts[0].id, "reject"))
    assert notifier.toasts[0][1] == "❌ Rejected"
    assert len(notifier.marks) == 1


async def test_double_tap_already(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "approve"))
    await process_callback(session, notifier, _photo_cb(posts[0].id, "approve"))
    assert notifier.toasts[1][1].startswith("Already")
    # mark_decided still called (idempotent edit clears stale button)
    assert len(notifier.marks) == 2


async def test_unknown_post(session: AsyncSession) -> None:
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(9999, "approve"))
    assert notifier.toasts[0][1] == "Post not found"
    assert len(notifier.marks) == 0


async def test_mark_decided_uses_caption_for_photo(session: AsyncSession) -> None:
    """process_callback picks editMessageCaption path for photo messages."""
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    cb = _photo_cb(posts[0].id, "approve")
    await process_callback(session, notifier, cb)
    # mark_decided got the photo message dict
    assert "photo" in notifier.marks[0][0]


async def test_mark_decided_uses_text_for_text_message(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    cb = _text_cb(posts[0].id, "reject")
    await process_callback(session, notifier, cb)
    assert "text" in notifier.marks[0][0]
    assert "photo" not in notifier.marks[0][0]


async def test_flip_approve_to_reject_callback(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "approve"))
    await process_callback(session, notifier, _photo_cb(posts[0].id, "reject"))
    assert notifier.toasts[1][1] == "❌ Rejected"
    assert notifier.marks[1][1].status == PostStatus.REJECTED


async def test_flip_reject_to_approve_callback(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "reject"))
    await process_callback(session, notifier, _photo_cb(posts[0].id, "approve"))
    assert notifier.toasts[1][1] == "✅ Approved"
    assert notifier.marks[1][1].status == PostStatus.APPROVED


def _msg(chat_id: int, text: str) -> dict:
    return {"chat": {"id": chat_id}, "text": text}


_OWNER_ID = 42
_OWNER_SETTINGS = Settings(
    telegram_bot_token="tok",
    telegram_chat_id=str(_OWNER_ID),
    stock_images_dir="stock",
    brand_file="brand.md",
)


async def test_status_owner_gets_summary(session: AsyncSession) -> None:
    await generate.generate_batch(session, n=2, brand="b")
    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(_OWNER_ID, "/status"), _OWNER_SETTINGS)
    assert len(notifier.messages) == 1
    assert "suggested" in notifier.messages[0]
    assert "2" in notifier.messages[0]


async def test_status_non_owner_refused(session: AsyncSession) -> None:
    await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(999, "/status"), _OWNER_SETTINGS)
    assert notifier.messages == []


async def test_non_command_ignored(session: AsyncSession) -> None:
    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(_OWNER_ID, "hello"), _OWNER_SETTINGS)
    assert notifier.messages == []


async def test_published_post_cannot_be_flipped_callback(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await session.refresh(posts[0])
    posts[0].status = PostStatus.PUBLISHED
    await session.commit()
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "reject"))
    assert "Can't change" in notifier.toasts[0][1]
    assert notifier.marks[0][1].status == PostStatus.PUBLISHED

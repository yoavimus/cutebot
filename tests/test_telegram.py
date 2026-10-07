"""Offline tests for process_callback, process_message, and mark_decided."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import llm, stock
from app.config import Settings
from app.models import BannedImage, Feedback, Post, PostStatus
from app.notifier.telegram import process_callback, process_message
from app.pipeline import generate, review
from app.schemas import PostSuggestion


class _FakeNotifier:
    def __init__(self) -> None:
        self.marks: list[tuple[dict, Post]] = []
        self.toasts: list[tuple[str, str]] = []
        self.messages: list[str] = []
        self.suggestions: list[Post] = []
        self.reason_pickers: list[tuple[dict, int]] = []
        self.edit_prompts: list[tuple[dict, int]] = []
        self.edited: list[tuple[int, int, Post]] = []

    async def send_suggestion(self, post: Post) -> None:
        self.suggestions.append(post)

    async def mark_decided(self, cb_message: dict, post: Post, reason: str | None = None) -> None:
        self.marks.append((cb_message, post))

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        self.toasts.append((callback_query_id, text))

    async def send_message(self, text: str) -> None:
        self.messages.append(text)

    async def show_reason_picker(self, cb_message: dict, post_id: int) -> None:
        self.reason_pickers.append((cb_message, post_id))

    async def prompt_edit(self, cb_message: dict, post_id: int) -> None:
        self.edit_prompts.append((cb_message, post_id))

    async def show_edited(self, chat_id: int, card_id: int, post: Post) -> None:
        self.edited.append((chat_id, card_id, post))


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


def _reason_cb(post_id: int, reason: str) -> dict:
    return {
        "id": "cq3",
        "data": f"reason:{post_id}:{reason}",
        "message": {
            "message_id": 11,
            "chat": {"id": 99},
            "text": "original text\n\nWhy reject?",
        },
    }


def _msg(chat_id: int, text: str) -> dict:
    return {"chat": {"id": chat_id}, "text": text}


_OWNER_ID = 42
_OWNER_SETTINGS = Settings(
    telegram_bot_token="tok",
    telegram_chat_id=str(_OWNER_ID),
    stock_images_dir="stock",
    brand_file="brand.md",
)


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_caption(
        brand: str, image_path: Path, settings: Settings, context: object = None
    ) -> PostSuggestion:
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


# ─────────────────────────────── approve tests ───────────────────────────────


async def test_fresh_approve(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "approve"))
    assert notifier.toasts[0][1] == "✅ Approved"
    assert len(notifier.marks) == 1
    assert notifier.marks[0][1].status == PostStatus.APPROVED


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
    assert "photo" in notifier.marks[0][0]


# ─────────────────────────────── reject / reason tests ───────────────────────


async def test_reject_shows_reason_picker(session: AsyncSession) -> None:
    """❌ tap → reason picker, no DB change yet."""
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "reject"))
    assert notifier.toasts[0][1] == "Why reject?"
    assert len(notifier.reason_pickers) == 1
    assert len(notifier.marks) == 0  # no mark_decided until reason is chosen
    # Post still SUGGESTED
    await session.refresh(posts[0])
    assert posts[0].status == PostStatus.SUGGESTED


async def test_fresh_reject_with_reason(session: AsyncSession) -> None:
    """Two-step reject: ❌ → reason chip → REJECTED with reason."""
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "reject"))
    await process_callback(session, notifier, _reason_cb(posts[0].id, "voice"))
    assert notifier.toasts[1][1] == "❌ Rejected (voice)"
    assert len(notifier.marks) == 1
    assert notifier.marks[0][1].status == PostStatus.REJECTED


async def test_fresh_reject_skip(session: AsyncSession) -> None:
    """Skip reason → REJECTED, no reason suffix in toast."""
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _text_cb(posts[0].id, "reject"))
    await process_callback(session, notifier, _reason_cb(posts[0].id, "skip"))
    assert notifier.toasts[1][1] == "❌ Rejected"
    assert notifier.marks[0][1].status == PostStatus.REJECTED


async def test_mark_decided_uses_text_for_text_message(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _text_cb(posts[0].id, "reject"))
    await process_callback(session, notifier, _reason_cb(posts[0].id, "skip"))
    assert "text" in notifier.marks[0][0]
    assert "photo" not in notifier.marks[0][0]


async def test_flip_approve_to_reject_callback(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "approve"))
    await process_callback(session, notifier, _photo_cb(posts[0].id, "reject"))
    assert notifier.toasts[1][1] == "Why reject?"
    await process_callback(session, notifier, _reason_cb(posts[0].id, "image"))
    assert notifier.toasts[2][1] == "❌ Rejected (image)"
    assert notifier.marks[-1][1].status == PostStatus.REJECTED


async def test_flip_reject_to_approve_callback(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "reject"))
    await process_callback(session, notifier, _reason_cb(posts[0].id, "boring"))
    assert notifier.marks[0][1].status == PostStatus.REJECTED
    await process_callback(session, notifier, _photo_cb(posts[0].id, "approve"))
    assert notifier.toasts[-1][1] == "✅ Approved"
    assert notifier.marks[-1][1].status == PostStatus.APPROVED


async def test_published_post_cannot_be_flipped_callback(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await session.refresh(posts[0])
    posts[0].status = PostStatus.PUBLISHED
    await session.commit()
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "reject"))
    assert "Can't change" in notifier.toasts[0][1]
    assert notifier.marks[0][1].status == PostStatus.PUBLISHED


async def test_ban_image_reason_bans_the_photo(session: AsyncSession) -> None:
    """🚫 Ban image chip → REJECTED with reason + the image is excluded from future picks."""
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _reason_cb(posts[0].id, "ban_image"))
    assert posts[0].status == PostStatus.REJECTED
    assert "banned" in notifier.toasts[0][1]
    assert await session.get(BannedImage, posts[0].image_ref) is not None


# ─────────────────────────────── /status command ─────────────────────────────


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


async def test_help_lists_every_command(session: AsyncSession) -> None:
    from app.notifier.telegram import _COMMANDS

    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(_OWNER_ID, "/help"), _OWNER_SETTINGS)
    assert all(cmd in notifier.messages[0] for cmd in _COMMANDS if cmd != "/start")


async def test_stock_command_and_unban(session: AsyncSession, tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff")
    settings = _OWNER_SETTINGS.model_copy(update={"stock_images_dir": str(tmp_path)})
    await stock.ban_image(session, "a.jpg")
    await session.commit()

    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(_OWNER_ID, "/stock"), settings)
    assert "1 banned" in notifier.messages[0] and "a.jpg" in notifier.messages[0]

    await process_message(session, notifier, _msg(_OWNER_ID, "/stock unban a.jpg"), settings)
    assert "back in the pool" in notifier.messages[1]
    await process_message(session, notifier, _msg(_OWNER_ID, "/stock unban a.jpg"), settings)
    assert "not banned" in notifier.messages[2]


# ─────────────────────────────── /generate command ───────────────────────────


async def test_cmd_generate_default_batch(session: AsyncSession) -> None:
    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(_OWNER_ID, "/generate"), _OWNER_SETTINGS)
    assert "Generated" in notifier.messages[0]
    assert len(notifier.suggestions) == _OWNER_SETTINGS.batch_size


async def test_cmd_generate_custom_n(session: AsyncSession) -> None:
    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(_OWNER_ID, "/generate 2"), _OWNER_SETTINGS)
    assert "2 post(s)" in notifier.messages[0]
    assert len(notifier.suggestions) == 2


# ─────────────────────────────── /queue command ──────────────────────────────


async def test_cmd_queue_empty(session: AsyncSession) -> None:
    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(_OWNER_ID, "/queue"), _OWNER_SETTINGS)
    assert notifier.messages[0] == "Queue is empty."


async def test_cmd_queue_with_posts(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    await review.handle_decision(session, posts[0].id, "approve")
    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(_OWNER_ID, "/queue"), _OWNER_SETTINGS)
    assert f"#{posts[0].id}" in notifier.messages[0]
    assert "1 post(s)" in notifier.messages[0]


# ─────────────────────────────── /requeue command ────────────────────────────


async def test_cmd_requeue(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    posts[0].status = PostStatus.FAILED
    await session.commit()
    notifier = _FakeNotifier()
    cmd = _msg(_OWNER_ID, f"/requeue {posts[0].id}")
    await process_message(session, notifier, cmd, _OWNER_SETTINGS)
    assert "requeued" in notifier.messages[0]
    await session.refresh(posts[0])
    assert posts[0].status == PostStatus.APPROVED


async def test_cmd_requeue_not_failed(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    cmd = _msg(_OWNER_ID, f"/requeue {posts[0].id}")
    await process_message(session, notifier, cmd, _OWNER_SETTINGS)
    assert "not failed" in notifier.messages[0]


# ─────────────────────────────── /pending command ────────────────────────────


async def test_cmd_pending_no_pending(session: AsyncSession) -> None:
    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(_OWNER_ID, "/pending"), _OWNER_SETTINGS)
    assert notifier.messages[0] == "No pending posts."


async def test_cmd_pending_resends(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=2, brand="b")
    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(_OWNER_ID, "/pending"), _OWNER_SETTINGS)
    assert "2 pending" in notifier.messages[0]
    assert len(notifier.suggestions) == len(posts)


# ── ✏️ Fix (M8.2) ────────────────────────────────────────────────────────────


def _fix_reply(post_id: int, text: str, *, chat_id: int = _OWNER_ID, is_bot: bool = True) -> dict:
    prompt = (
        "Reply to this message with the corrected Hebrew caption "
        f'(post #{post_id} · card 77). Start with "en:" to fix the English instead.'
    )
    return {
        "chat": {"id": chat_id},
        "text": text,
        "reply_to_message": {"from": {"is_bot": is_bot}, "text": prompt},
    }


async def _feedback(session: AsyncSession, post_id: int) -> list[Feedback]:
    rows = await session.scalars(select(Feedback).where(Feedback.post_id == post_id))
    return list(rows)


async def test_fix_button_prompts_for_edit(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "fix"))
    assert notifier.edit_prompts == [(_photo_cb(0, "x")["message"], posts[0].id)]
    assert await _feedback(session, posts[0].id) == []  # asking changes nothing


async def test_fix_button_refused_when_published(session: AsyncSession) -> None:
    posts = await generate.generate_batch(session, n=1, brand="b")
    posts[0].status = PostStatus.PUBLISHED
    await session.commit()
    notifier = _FakeNotifier()
    await process_callback(session, notifier, _photo_cb(posts[0].id, "fix"))
    assert notifier.edit_prompts == []
    assert "Can't edit" in notifier.toasts[0][1]


async def test_he_reply_approves_suggested_with_delta(session: AsyncSession) -> None:
    post = (await generate.generate_batch(session, n=1, brand="b"))[0]
    old_he, old_en = post.caption_he, post.caption_en
    notifier = _FakeNotifier()
    await process_message(session, notifier, _fix_reply(post.id, "כיתוב מתוקן"), _OWNER_SETTINGS)
    await session.refresh(post)
    assert post.status == PostStatus.APPROVED and post.queue_position is not None
    assert (post.caption_he, post.caption_en) == ("כיתוב מתוקן", old_en)
    (fb,) = await _feedback(session, post.id)
    assert (fb.decision, fb.edit_lang, fb.edit_before, fb.edit_after) == (
        "approve",
        "he",
        old_he,
        "כיתוב מתוקן",
    )
    assert [(c, m) for c, m, _ in notifier.edited] == [(_OWNER_ID, 77)]


async def test_en_prefix_fixes_english(session: AsyncSession) -> None:
    post = (await generate.generate_batch(session, n=1, brand="b"))[0]
    old_he = post.caption_he
    await process_message(
        session, _FakeNotifier(), _fix_reply(post.id, "EN: better caption"), _OWNER_SETTINGS
    )
    await session.refresh(post)
    assert (post.caption_he, post.caption_en) == (old_he, "better caption")
    (fb,) = await _feedback(session, post.id)
    assert (fb.edit_lang, fb.edit_after) == ("en", "better caption")


async def test_edit_of_approved_keeps_queue_position(session: AsyncSession) -> None:
    post = (await generate.generate_batch(session, n=1, brand="b"))[0]
    await review.handle_decision(session, post.id, "approve")
    await session.refresh(post)
    pos = post.queue_position
    await process_message(session, _FakeNotifier(), _fix_reply(post.id, "תיקון"), _OWNER_SETTINGS)
    await session.refresh(post)
    assert post.status == PostStatus.APPROVED and post.queue_position == pos
    assert post.caption_he == "תיקון"
    assert len(await _feedback(session, post.id)) == 2  # the approve + the edit


async def test_edit_refused_for_published_and_rejected(session: AsyncSession) -> None:
    for status in (PostStatus.PUBLISHED, PostStatus.REJECTED):
        post = (await generate.generate_batch(session, n=1, brand="b"))[0]
        old = post.caption_he
        post.status = status
        await session.commit()
        notifier = _FakeNotifier()
        await process_message(session, notifier, _fix_reply(post.id, "nope"), _OWNER_SETTINGS)
        await session.refresh(post)
        assert post.caption_he == old and post.status == status
        assert notifier.edited == [] and "can't edit" in notifier.messages[0]
        assert await _feedback(session, post.id) == []


async def test_unchanged_and_empty_edit_are_noops(session: AsyncSession) -> None:
    post = (await generate.generate_batch(session, n=1, brand="b"))[0]
    notifier = _FakeNotifier()
    await process_message(session, notifier, _fix_reply(post.id, post.caption_he), _OWNER_SETTINGS)
    await process_message(session, notifier, _fix_reply(post.id, "en:"), _OWNER_SETTINGS)
    await session.refresh(post)
    assert post.status == PostStatus.SUGGESTED
    assert "unchanged" in notifier.messages[0] and "Empty" in notifier.messages[1]


async def test_edit_from_non_owner_or_non_bot_prompt_ignored(session: AsyncSession) -> None:
    post = (await generate.generate_batch(session, n=1, brand="b"))[0]
    notifier = _FakeNotifier()
    await process_message(session, notifier, _fix_reply(post.id, "x", chat_id=999), _OWNER_SETTINGS)
    await process_message(
        session, notifier, _fix_reply(post.id, "x", is_bot=False), _OWNER_SETTINGS
    )
    await session.refresh(post)
    assert post.status == PostStatus.SUGGESTED and post.caption_he != "x"
    assert notifier.messages == [] and notifier.edited == []


async def test_status_shows_learning_counts(session: AsyncSession) -> None:
    post = (await generate.generate_batch(session, n=1, brand="b"))[0]
    await process_message(session, _FakeNotifier(), _fix_reply(post.id, "תיקון"), _OWNER_SETTINGS)
    notifier = _FakeNotifier()
    await process_message(session, notifier, _msg(_OWNER_ID, "/status"), _OWNER_SETTINGS)
    assert "learning: 1 examples · 1 edits · 0 recent · 0 reject hints" in notifier.messages[0]

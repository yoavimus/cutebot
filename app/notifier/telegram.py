"""Telegram review adapter — DMs suggestions with inline Approve/Reject buttons.

Callback data is ``approve:<post_id>`` / ``reject:<post_id>``. The FastAPI webhook
(``app/main.py``) parses callbacks and routes them to ``process_callback``.

Run modes (CLI):
    python -m app.notifier.telegram poll           # long-poll (no public URL needed)
    python -m app.notifier.telegram set-webhook    # register the webhook
    python -m app.notifier.telegram delete-webhook
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Post, PostStatus
from app.pipeline.review import handle_decision
from app.render import image_path, render_full_caption

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
# Telegram photo captions are capped at 1024 chars; longer captions go in a follow-up text.
_PHOTO_CAPTION_LIMIT = 1024


class TelegramNotifier:
    """Concrete :class:`app.notifier.base.Notifier` backed by the Telegram Bot API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def _url(self, method: str) -> str:
        return _API.format(token=self._settings.telegram_bot_token, method=method)

    async def send_suggestion(self, post: Post) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            logger.warning("Telegram not configured — skipping send for post %s.", post.id)
            return
        caption = render_full_caption(post, self._settings)
        photo_path = image_path(post, self._settings)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{post.id}"},
                    {"text": "❌ Reject", "callback_data": f"reject:{post.id}"},
                ]
            ]
        }
        async with httpx.AsyncClient(timeout=15) as client:
            if len(caption) <= _PHOTO_CAPTION_LIMIT:
                await client.post(
                    self._url("sendPhoto"),
                    data={
                        "chat_id": self._settings.telegram_chat_id,
                        "caption": caption,
                        "reply_markup": json.dumps(keyboard),
                    },
                    files={"photo": photo_path.read_bytes()},
                )
            else:
                # Caption too long for a photo caption — send the photo bare, then the full
                # text with the controls, so the buttons always sit with the full caption.
                await client.post(
                    self._url("sendPhoto"),
                    data={"chat_id": self._settings.telegram_chat_id},
                    files={"photo": photo_path.read_bytes()},
                )
                await client.post(
                    self._url("sendMessage"),
                    json={
                        "chat_id": self._settings.telegram_chat_id,
                        "text": caption,
                        "reply_markup": keyboard,
                    },
                )

    async def send_message(self, text: str) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            logger.warning("Telegram not configured — skipping send_message.")
            return
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                self._url("sendMessage"),
                json={"chat_id": self._settings.telegram_chat_id, "text": text},
            )

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                self._url("answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
            )

    async def mark_decided(self, cb_message: dict, post: Post) -> None:
        """Edit the reviewed message; leave the opposite button while decision is reversible."""
        if post.status == PostStatus.APPROVED:
            label = "✅ Approved"
            keyboard = {"inline_keyboard": [[
                {"text": "↩︎ Reject", "callback_data": f"reject:{post.id}"}
            ]]}
        elif post.status == PostStatus.REJECTED:
            label = "❌ Rejected"
            keyboard = {"inline_keyboard": [[
                {"text": "↩︎ Approve", "callback_data": f"approve:{post.id}"}
            ]]}
        else:
            label = "✅ Published" if post.status == PostStatus.PUBLISHED else f"[{post.status}]"
            keyboard = {"inline_keyboard": []}
        chat_id = cb_message["chat"]["id"]
        message_id = cb_message["message_id"]
        is_photo = "photo" in cb_message
        endpoint = "editMessageCaption" if is_photo else "editMessageText"
        body_key = "caption" if is_photo else "text"
        original = cb_message.get(body_key, "")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    self._url(endpoint),
                    json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        body_key: f"{original}\n\n{label}",
                        "reply_markup": keyboard,
                    },
                )
            if not resp.json().get("ok"):
                logger.warning(
                    "mark_decided edit rejected: message=%s description=%s",
                    message_id,
                    resp.json().get("description"),
                )
        except Exception as exc:
            logger.warning("mark_decided failed: message=%s error=%s", message_id, exc)


_TERMINAL_CB = {PostStatus.PUBLISHING, PostStatus.PUBLISHED, PostStatus.FAILED}


async def _build_status_summary(session: AsyncSession) -> str:
    rows = (await session.execute(select(Post.status, func.count()).group_by(Post.status))).all()
    counts: dict[PostStatus, int] = {r[0]: r[1] for r in rows}
    recent = (
        await session.scalars(
            select(Post)
            .where(Post.status == PostStatus.PUBLISHED)
            .order_by(Post.published_at.desc())
            .limit(5)
        )
    ).all()
    lines = ["Pipeline status"]
    for s in PostStatus:
        n = counts.get(s, 0)
        if n:
            lines.append(f"  {s}: {n}")
    queue = counts.get(PostStatus.APPROVED, 0) + counts.get(PostStatus.PUBLISHING, 0)
    lines.append(f"\nQueue depth: {queue}")
    if recent:
        lines.append("\nLast published:")
        for p in recent:
            ts = p.published_at.strftime("%Y-%m-%d %H:%M UTC") if p.published_at else "?"
            lines.append(f"  #{p.id} — {ts}")
    return "\n".join(lines)


async def process_message(
    session: AsyncSession, notifier: TelegramNotifier, msg: dict, settings: Settings
) -> None:
    """Handle an incoming plain message. Only /status from the owner chat is acted on."""
    text = msg.get("text", "")
    if not text.startswith("/status"):
        return
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(settings.telegram_chat_id):
        logger.warning("Ignoring /status from non-owner chat %s.", chat_id)
        return
    summary = await _build_status_summary(session)
    await notifier.send_message(summary)


async def process_callback(session: AsyncSession, notifier: TelegramNotifier, cb: dict) -> None:
    """Parse a Telegram callback query, apply the decision, update the message, and toast."""
    parsed = parse_callback(cb.get("data", ""))
    if not parsed:
        return
    decision, post_id = parsed
    # ponytail: pre-fetch hits the identity map in handle_decision; no extra DB round-trip
    pre = await session.get(Post, post_id)
    pre_status = pre.status if pre is not None else None  # capture before handle_decision mutates
    post = await handle_decision(session, post_id, decision)

    if post is None:
        toast = "Post not found"
    elif pre_status in _TERMINAL_CB and pre_status == post.status:
        toast = "Can't change — already published"
    elif pre_status == post.status:
        toast = f"Already {post.status}"
    else:
        toast = "✅ Approved" if post.status == PostStatus.APPROVED else "❌ Rejected"

    if post is not None:
        await notifier.mark_decided(cb["message"], post)

    logger.info(
        "callback post_id=%s decision=%s status=%s toast=%r",
        post_id,
        decision,
        post.status if post else None,
        toast,
    )
    await notifier.answer_callback(cb["id"], toast)


def parse_callback(data: str) -> tuple[str, int] | None:
    """Parse ``approve:<id>`` / ``reject:<id>`` into ``(decision, post_id)``."""
    action, _, raw_id = data.partition(":")
    if action not in {"approve", "reject"} or not raw_id.isdigit():
        return None
    return action, int(raw_id)


# --------------------------------------------------------------------------- CLI


async def _set_webhook() -> None:
    settings = get_settings()
    url = f"{settings.telegram_webhook_base.rstrip('/')}/telegram/webhook"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _API.format(token=settings.telegram_bot_token, method="setWebhook"),
            json={"url": url, "secret_token": settings.telegram_webhook_secret},
        )
    logger.info("setWebhook -> %s", resp.json())


async def _delete_webhook() -> None:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _API.format(token=settings.telegram_bot_token, method="deleteWebhook"),
        )
    logger.info("deleteWebhook -> %s", resp.json())


async def _poll() -> None:
    """Long-poll for updates and route callbacks (local dev; no public URL needed)."""
    from app.db import SessionLocal

    settings = get_settings()
    notifier = TelegramNotifier(settings)
    offset = 0
    logger.info("Polling Telegram for review decisions… (Ctrl-C to stop)")
    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            resp = await client.get(
                _API.format(token=settings.telegram_bot_token, method="getUpdates"),
                params={"timeout": 30, "offset": offset},
            )
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                cb = update.get("callback_query")
                msg = update.get("message")
                if cb:
                    async with SessionLocal() as session:
                        await process_callback(session, notifier, cb)
                elif msg:
                    async with SessionLocal() as session:
                        await process_message(session, notifier, msg, settings)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "poll"
    runners = {
        "poll": _poll,
        "set-webhook": _set_webhook,
        "delete-webhook": _delete_webhook,
    }
    runner = runners.get(cmd)
    if runner is None:
        print(f"unknown command {cmd!r}; use: {', '.join(runners)}")
        raise SystemExit(2)
    asyncio.run(runner())


if __name__ == "__main__":
    main()

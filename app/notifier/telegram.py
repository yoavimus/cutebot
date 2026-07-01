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

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                self._url("answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
            )

    async def mark_decided(self, cb_message: dict, decision: str) -> None:
        """Edit the reviewed message to show the decision and remove the buttons."""
        label = "✅ Approved" if decision == "approve" else "❌ Rejected"
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
                        "reply_markup": {"inline_keyboard": []},
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


async def process_callback(session: AsyncSession, notifier: TelegramNotifier, cb: dict) -> None:
    """Parse a Telegram callback query, apply the decision, update the message, and toast."""
    parsed = parse_callback(cb.get("data", ""))
    if not parsed:
        return
    decision, post_id = parsed
    # ponytail: pre-fetch hits the identity map in handle_decision; no extra DB round-trip
    pre = await session.get(Post, post_id)
    was_fresh = pre is not None and pre.status == PostStatus.SUGGESTED
    post = await handle_decision(session, post_id, decision)

    if post is None:
        toast = "Post not found"
    elif was_fresh:
        toast = "✅ Approved" if decision == "approve" else "❌ Rejected"
    else:
        toast = f"Already {post.status}"

    if post is not None:
        await notifier.mark_decided(cb["message"], decision)

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
                if not cb:
                    continue
                async with SessionLocal() as session:
                    await process_callback(session, notifier, cb)


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

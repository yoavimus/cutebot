"""Telegram review adapter — DMs suggestions with inline Approve/Reject buttons.

Callback data is ``approve:<post_id>`` / ``reject:<post_id>``. The FastAPI webhook
(``app/main.py``) parses callbacks and routes them to ``pipeline.review.handle_decision``.

Run modes (CLI):
    python -m app.notifier.telegram poll           # long-poll (no public URL needed)
    python -m app.notifier.telegram set-webhook    # register the webhook
    python -m app.notifier.telegram delete-webhook
"""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx

from app.config import Settings, get_settings
from app.models import Post

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"


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
        text = (
            f"🆕 *Post suggestion #{post.id}*\n\n"
            f"{post.caption}\n\n"
            f"🎨 _{post.visual_concept}_\n\n"
            f"💡 {post.rationale}"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{post.id}"},
                    {"text": "❌ Reject", "callback_data": f"reject:{post.id}"},
                ]
            ]
        }
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                self._url("sendMessage"),
                json={
                    "chat_id": self._settings.telegram_chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                self._url("answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
            )


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
    from app.pipeline.review import handle_decision

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
                parsed = parse_callback(cb.get("data", ""))
                if not parsed:
                    continue
                decision, post_id = parsed
                async with SessionLocal() as session:
                    await handle_decision(session, post_id, decision)
                await notifier.answer_callback(cb["id"], f"Recorded: {decision} #{post_id}")


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

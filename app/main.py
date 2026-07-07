"""FastAPI app — HTTP surface (health, Telegram webhook, dev triggers) + scheduler host."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal, get_session, init_db
from app.models import Post
from app.notifier.telegram import TelegramNotifier, _set_webhook, process_callback
from app.pipeline import generate, publish, queue, review
from app.scheduler import build_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.is_dev:
        await init_db()
    async with SessionLocal() as session:
        await publish.recover_orphaned(session)
    notifier = TelegramNotifier(settings)
    scheduler = build_scheduler(SessionLocal, notifier, settings)
    scheduler.start()
    app.state.notifier = notifier
    app.state.scheduler = scheduler
    if not settings.is_dev and settings.telegram_webhook_base:
        await _set_webhook()
    logger.info("CuteBot started (env=%s).", settings.app_env)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="CuteBot", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Receive Telegram callback queries (Approve/Reject) and apply the decision."""
    settings = get_settings()
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="bad webhook secret")

    update = await request.json()
    cb = update.get("callback_query")
    if not cb:
        return {"ok": True, "ignored": "no callback_query"}

    notifier: TelegramNotifier = request.app.state.notifier
    await process_callback(session, notifier, cb)
    return {"ok": True}


# --------------------------------------------------------------- dev-only triggers


@app.post("/dev/generate")
async def dev_generate(
    request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    _require_dev()
    posts = await generate.generate_batch(session)
    await review.send_for_review(posts, request.app.state.notifier)
    return {"generated": [p.id for p in posts]}


@app.post("/dev/publish-next")
async def dev_publish_next(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_dev()
    post = await publish.publish_next(session)
    return {"published": post.id if post else None}


@app.post("/dev/requeue/{post_id}")
async def dev_requeue(post_id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_dev()
    post = await session.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    await queue.requeue(session, post)
    await session.commit()
    return {"requeued": post_id, "queue_position": post.queue_position}


def _require_dev() -> None:
    if not get_settings().is_dev:
        raise HTTPException(status_code=404, detail="not found")

"""FastAPI app — HTTP surface (health, Telegram webhook, dev triggers) + scheduler host."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import SessionLocal, get_session, init_db
from app.models import Post, PostStatus
from app.notifier.telegram import TelegramNotifier, _set_webhook, process_callback, process_message
from app.pipeline import generate, publish, queue, review
from app.scheduler import build_scheduler

logger = logging.getLogger(__name__)


def check_prod_config(settings: Settings) -> None:
    """Refuse to boot in production with the guessable default webhook secret."""
    if not settings.is_dev and settings.telegram_webhook_secret == "cutebot-webhook-secret":
        raise RuntimeError(
            "TELEGRAM_WEBHOOK_SECRET is still the default value — set a random secret "
            "before running in production (anyone could forge webhook callbacks)."
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    check_prod_config(settings)
    if settings.is_dev:
        await init_db()
    async with SessionLocal() as session:
        await publish.recover_orphaned(session)
        await publish.catch_up_missed_slot(session, settings)
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
    notifier: TelegramNotifier = request.app.state.notifier
    cb = update.get("callback_query")
    msg = update.get("message")
    if cb:
        await process_callback(session, notifier, cb)
    elif msg:
        await process_message(session, notifier, msg, settings)
    else:
        return {"ok": True, "ignored": "no callback_query or message"}
    return {"ok": True}


# --------------------------------------------------------------- dev-only triggers


def _post_summary(post: Post) -> dict[str, Any]:
    """Compact post dict for dev responses."""
    he = post.caption_he
    en = post.caption_en
    return {
        "id": post.id,
        "status": post.status,
        "queue_position": post.queue_position,
        "batch_id": post.batch_id,
        "decided_at": post.decided_at.isoformat() if post.decided_at else None,
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "image_ref": post.image_ref,
        "caption_he": (he[:80] + "…") if len(he) > 80 else he,
        "caption_en": (en[:80] + "…") if len(en) > 80 else en,
    }


@app.get("/dev/status")
async def dev_status(
    status: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_dev()
    q = select(Post).order_by(Post.id.desc()).limit(20)
    if status:
        try:
            q = q.where(Post.status == PostStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown status {status!r}") from None
    posts = (await session.scalars(q)).all()
    return {"posts": [_post_summary(p) for p in posts]}


@app.post("/dev/generate")
async def dev_generate(
    request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    _require_dev()
    posts = await generate.generate_batch(session)
    await review.send_for_review(posts, request.app.state.notifier)
    return {"generated": [_post_summary(p) for p in posts]}


@app.post("/dev/publish-next")
async def dev_publish_next(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_dev()
    post = await publish.publish_next(session)
    return {"published": _post_summary(post) if post else None}


@app.post("/dev/run-cycle")
async def dev_run_cycle(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Generate → auto-approve → publish. One-shot dev cycle; never reachable in prod."""
    _require_dev()
    posts = await generate.generate_batch(session)
    generated = [_post_summary(p) for p in posts]
    for p in posts:
        await review.handle_decision(session, p.id, "approve")
    published: list[dict[str, Any]] = []
    while True:
        post = await publish.publish_next(session)
        if post is None:
            break
        published.append(_post_summary(post))
    return {"generated": generated, "published": published}


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

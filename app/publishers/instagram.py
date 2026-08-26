"""Instagram Graph API publisher — real single-image feed adapter.

Two Graph calls plus a poll, keyed by a public image URL (served at ``GET
/media/{image_ref}``, see ``app.main``) and a long-lived token:

1. Create container — ``POST /{ig-user-id}/media`` (image_url, caption) -> container id.
2. Poll ``GET /{container-id}?fields=status_code`` until FINISHED/ERROR/EXPIRED.
3. Publish — ``POST /{ig-user-id}/media_publish`` (creation_id) -> media id.

Idempotency markers (``Post.ig_container_id`` / ``Post.ig_media_id``) are committed
mid-flight so a crash-restart can tell what already happened — see
``app.pipeline.publish.recover_orphaned``. See M7_PLAN.md § M7.1/M7.3.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Post
from app.publishers.base import PublishResult
from app.render import image_path, render_full_caption

logger = logging.getLogger(__name__)

# Instagram-Login flow: post straight to the IG business account (no FB Page).
# ponytail: host hardcoded — single-brand, one flow. Make it a setting only if a
# Page-linked (graph.facebook.com) account is ever added alongside this one.
_GRAPH_BASE = "https://graph.instagram.com"
_POLL_TRIES = 10
_POLL_DELAY_S = 3
_DONE_STATUSES = {"FINISHED", "ERROR", "EXPIRED", "PUBLISHED"}

# IG feed image spec: https://developers.facebook.com/docs/instagram-platform/content-publishing
_MIN_WIDTH, _MAX_WIDTH = 320, 1440
_MIN_ASPECT, _MAX_ASPECT = 4 / 5, 1.91
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


async def _graph_post(path: str, data: dict[str, Any], settings: Settings) -> dict[str, Any]:
    url = f"{_GRAPH_BASE}/{settings.instagram_graph_version}/{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data=data)
    return dict(resp.json())


async def _graph_get(path: str, params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    url = f"{_GRAPH_BASE}/{settings.instagram_graph_version}/{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params)
    return dict(resp.json())


def _graph_error(body: dict[str, Any]) -> str | None:
    err = body.get("error")
    if not err:
        return None
    return f"{err.get('message', 'unknown error')} (code {err.get('code', '?')})"


def _validate_image(post: Post, settings: Settings) -> str | None:
    """Check the stock image against IG's feed-image spec. Returns an error, or None.

    ponytail: validates the source file (dimensions survive JPEG re-encode; the size
    check is a conservative proxy for the served bytes since /media re-encodes on the
    fly). Tighten if a source ever slips past this and gets rejected by Graph.
    """
    path = image_path(post, settings)
    try:
        with Image.open(path) as img:
            width, height = img.size
    except Exception as exc:  # noqa: BLE001 — surfaced as a failed PublishResult, not a crash
        return f"cannot read image {path}: {exc}"
    if not (_MIN_WIDTH <= width <= _MAX_WIDTH):
        return f"image width {width}px out of Instagram's range [{_MIN_WIDTH}, {_MAX_WIDTH}]"
    aspect = width / height
    if not (_MIN_ASPECT - 1e-6 <= aspect <= _MAX_ASPECT + 1e-6):
        return (
            f"image aspect ratio {aspect:.3f} out of Instagram's range "
            f"[{_MIN_ASPECT:.2f}, {_MAX_ASPECT}]"
        )
    size = path.stat().st_size
    if size > _MAX_IMAGE_BYTES:
        return f"image size {size} bytes exceeds Instagram's {_MAX_IMAGE_BYTES}-byte max"
    return None


async def get_container_status(container_id: str, settings: Settings) -> str:
    """Query a container's ``status_code`` — used by both the publish poll and recovery."""
    body = await _graph_get(
        container_id,
        {"fields": "status_code", "access_token": settings.instagram_access_token},
        settings,
    )
    err = _graph_error(body)
    if err:
        raise RuntimeError(f"container status query failed: {err}")
    return str(body.get("status_code", "UNKNOWN"))


async def _poll_container(container_id: str, settings: Settings) -> str:
    for _ in range(_POLL_TRIES):
        status = await get_container_status(container_id, settings)
        if status in _DONE_STATUSES:
            return status
        await asyncio.sleep(_POLL_DELAY_S)
    return "TIMEOUT"


class InstagramPublisher:
    """Real Instagram Graph adapter — single-image feed posts."""

    name = "instagram"

    async def publish(self, session: AsyncSession, post: Post) -> PublishResult:
        settings = get_settings()

        validation_error = _validate_image(post, settings)
        if validation_error:
            logger.error("Instagram validation failed for post %s: %s", post.id, validation_error)
            return PublishResult(network=self.name, ok=False, detail=validation_error)

        if not post.ig_container_id:
            body = await _graph_post(
                f"{settings.instagram_ig_user_id}/media",
                {
                    "image_url": f"{settings.public_base_url}/media/{post.image_ref}",
                    "caption": render_full_caption(post, settings),
                    "access_token": settings.instagram_access_token,
                },
                settings,
            )
            err = _graph_error(body)
            if err:
                return PublishResult(network=self.name, ok=False, detail=f"container create: {err}")
            post.ig_container_id = str(body["id"])
            await session.commit()

        status = await _poll_container(post.ig_container_id, settings)
        if status not in {"FINISHED", "PUBLISHED"}:
            return PublishResult(network=self.name, ok=False, detail=f"container status: {status}")

        body = await _graph_post(
            f"{settings.instagram_ig_user_id}/media_publish",
            {"creation_id": post.ig_container_id, "access_token": settings.instagram_access_token},
            settings,
        )
        err = _graph_error(body)
        if err:
            return PublishResult(network=self.name, ok=False, detail=f"media_publish: {err}")
        post.ig_media_id = str(body["id"])
        await session.commit()
        return PublishResult(network=self.name, ok=True, detail=post.ig_media_id)

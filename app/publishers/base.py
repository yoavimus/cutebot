"""Publisher interface + v1 stub adapters.

A publisher broadcasts one approved post to one network. v1 ships logging stubs that
record instead of posting; replace each ``publish`` body with the real API integration
(roadmap §E) without touching pipeline code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.models import Post

logger = logging.getLogger(__name__)


@dataclass
class PublishResult:
    network: str
    ok: bool
    detail: str = ""


@runtime_checkable
class Publisher(Protocol):
    """Broadcasts a post to a single social network."""

    name: str

    async def publish(self, post: Post) -> PublishResult:
        ...


class _LoggingStubPublisher:
    """Base stub — logs the post instead of calling the network API."""

    name = "stub"

    async def publish(self, post: Post) -> PublishResult:
        logger.info("[%s] would publish post #%s: %s", self.name, post.id, post.caption)
        return PublishResult(network=self.name, ok=True, detail="stub: logged, not sent")


class InstagramPublisher(_LoggingStubPublisher):
    name = "instagram"


class TikTokPublisher(_LoggingStubPublisher):
    name = "tiktok"


class XPublisher(_LoggingStubPublisher):
    name = "x"


def get_publishers() -> list[Publisher]:
    """The active publisher set. v1: stubs for Instagram, TikTok, and X."""
    return [InstagramPublisher(), TikTokPublisher(), XPublisher()]

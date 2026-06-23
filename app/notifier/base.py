"""Notifier interface — the review channel abstraction.

A notifier sends a single post suggestion to the human reviewer with Approve/Reject
controls. Concrete adapters (Telegram, Discord, Slack) implement this; pipeline code
depends only on the interface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models import Post


@runtime_checkable
class Notifier(Protocol):
    """Sends post suggestions to the reviewer and surfaces their decision."""

    async def send_suggestion(self, post: Post) -> None:
        """Deliver one suggestion with Approve/Reject controls."""
        ...

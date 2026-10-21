"""Notifier interface — the review channel abstraction.

A notifier sends a single post suggestion to the human reviewer with Approve/Reject
controls. Concrete adapters (Telegram, Discord, Slack) implement this; pipeline code
depends only on the interface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models import Brand, Post


@runtime_checkable
class Notifier(Protocol):
    """Sends post suggestions to the reviewer and surfaces their decision.

    A notifier is addressed to one brand's owner; ``for_brand`` returns a copy addressed to
    another brand (the process-wide instance falls back to the env chat for boot warnings).
    """

    def for_brand(self, brand: Brand) -> Notifier:
        """The same channel, addressed to ``brand``'s owner."""
        ...

    async def send_suggestion(self, post: Post) -> None:
        """Deliver one suggestion with Approve/Reject controls."""
        ...

    async def send_message(
        self,
        text: str,
        *,
        html: bool = False,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Send a message to the owner (status updates, alerts); optional HTML + inline buttons."""
        ...

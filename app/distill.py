"""Brand distillation (M8.4): ``/distill`` proposes a revision of the brand text from recent
feedback; nothing is written until the owner taps ✅ Apply.

The model returns the complete revised file (structured); the unified diff shown to the
owner is computed here with ``difflib`` — more reliable than asking a model to emit a patch.
ponytail: pending proposals live in-process (lost on restart → "expired, run /distill
again"); persist them only if restarts between propose and tap ever bite.
"""

from __future__ import annotations

import difflib
import secrets
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app import brands, learning, llm
from app.config import Settings
from app.models import Brand


@dataclass
class Proposal:
    token: str
    diff: str
    rationale: str


_PENDING: dict[str, tuple[int, str, str]] = {}  # token -> (brand id, base text, new text)


async def propose(session: AsyncSession, settings: Settings, brand: Brand) -> Proposal | str:
    """A ``Proposal`` to DM, or a plain-text reason there's nothing to propose.

    Evidence and text are both ``brand``'s: the proposal revises ``brand.brand_text``.
    """
    ctx = await learning.build_context(session, brand.id, settings)
    if ctx.is_empty:
        return "Nothing to learn from yet — no approvals, edits, or rejections in the window."
    current = brands.text(brand)
    result = await llm.distill_brand(current, learning.render(ctx), settings)
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            result.new_brand.splitlines(keepends=True),
            "brand",
            "brand (proposed)",
        )
    )
    if not diff:
        return f"No changes proposed. {result.rationale}"
    token = secrets.token_hex(4)
    _PENDING[token] = (brand.id, current, result.new_brand)
    return Proposal(token, diff, result.rationale)


async def apply(session: AsyncSession, brand: Brand, token: str) -> str:
    """Write the proposed text to the brand row (old text -> ``brand_text_prev``)."""
    pending = _PENDING.pop(token, None)
    if pending is None or pending[0] != brand.id:
        return "Proposal expired — run /distill again."
    _, base, new = pending
    if brands.text(brand) != base:
        return "Brand text changed since this proposal — run /distill again."
    brand.brand_text_prev = brand.brand_text
    brand.brand_text = new
    await session.commit()
    return "✅ Applied — brand text updated (previous kept as brand_text_prev)."


def discard(token: str) -> str:
    _PENDING.pop(token, None)
    return "❌ Discarded — brand text untouched."

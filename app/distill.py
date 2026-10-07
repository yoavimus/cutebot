"""Brand distillation (M8.4): ``/distill`` proposes a ``brand.md`` revision from recent
feedback; nothing is written until the owner taps ✅ Apply.

The model returns the complete revised file (structured); the unified diff shown to the
owner is computed here with ``difflib`` — more reliable than asking a model to emit a patch.
ponytail: pending proposals live in-process (lost on restart → "expired, run /distill
again"); persist them only if restarts between propose and tap ever bite.
"""

from __future__ import annotations

import difflib
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app import learning, llm
from app.brand import load_brand
from app.config import Settings


@dataclass
class Proposal:
    token: str
    diff: str
    rationale: str


_PENDING: dict[str, tuple[str, str]] = {}  # token -> (base brand text, new brand text)


async def propose(session: AsyncSession, settings: Settings) -> Proposal | str:
    """A ``Proposal`` to DM, or a plain-text reason there's nothing to propose."""
    ctx = await learning.build_context(session, settings)
    if ctx.is_empty:
        return "Nothing to learn from yet — no approvals, edits, or rejections in the window."
    brand = load_brand()
    result = await llm.distill_brand(brand, learning.render(ctx), settings)
    diff = "".join(
        difflib.unified_diff(
            brand.splitlines(keepends=True),
            result.new_brand.splitlines(keepends=True),
            "brand.md",
            "brand.md (proposed)",
        )
    )
    if not diff:
        return f"No changes proposed. {result.rationale}"
    token = secrets.token_hex(4)
    _PENDING[token] = (brand, result.new_brand)
    return Proposal(token, diff, result.rationale)


def apply(token: str, settings: Settings) -> str:
    """Write the proposed brand (backing up to ``<brand_file>.bak``). Returns a status line."""
    pending = _PENDING.pop(token, None)
    if pending is None:
        return "Proposal expired — run /distill again."
    base, new = pending
    if load_brand() != base:
        return "brand.md changed since this proposal — run /distill again."
    path = Path(settings.brand_file)
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + ".bak"))
    path.write_text(new, encoding="utf-8")
    return "✅ Applied — brand.md updated (backup: brand.md.bak)."


def discard(token: str) -> str:
    _PENDING.pop(token, None)
    return "❌ Discarded — brand.md untouched."

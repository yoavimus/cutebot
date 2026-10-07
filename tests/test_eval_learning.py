"""M8.3 — `eval_models --learning` builds the context from the DB and passes it through."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, llm, stock
from app.config import Settings
from app.learning import LearningContext
from app.models import Post, PostStatus
from app.schemas import PostSuggestion
from scripts import eval_models


async def _run(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, learning: bool
) -> tuple[list[LearningContext | None], str]:
    seen: list[LearningContext | None] = []

    async def fake_caption(
        brand: str, image: Path, settings: Settings, context: LearningContext | None = None
    ) -> PostSuggestion:
        seen.append(context)
        return PostSuggestion(caption_he="ה", caption_en="e", visual_concept="v")

    class _Sessions:  # stands in for app.db.SessionLocal: `async with SessionLocal() as s`
        def __call__(self) -> AsyncSession:
            return session

    monkeypatch.setattr(llm, "caption_image", fake_caption)
    monkeypatch.setattr(db, "SessionLocal", _Sessions())
    monkeypatch.setattr(stock, "list_images", lambda s: [Path("a.jpg"), Path("b.jpg")])
    monkeypatch.setattr(eval_models, "load_brand", lambda: "brand")
    out = tmp_path / "out.md"
    await eval_models.run(["m/x"], 2, str(out), learning)
    return seen, out.read_text(encoding="utf-8")


async def test_learning_flag_passes_db_context(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session.add(
        Post(
            image_ref="1.jpg",
            caption_he="אושר",
            caption_en="ok",
            visual_concept="v",
            status=PostStatus.APPROVED,
        )
    )
    await session.commit()
    seen, md = await _run(session, monkeypatch, tmp_path, learning=True)
    assert all(c is not None and [e.caption_he for e in c.examples] == ["אושר"] for c in seen)
    assert "Learning context: **ON** — 1 examples" in md


async def test_default_stays_context_free(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen, md = await _run(session, monkeypatch, tmp_path, learning=False)
    assert seen == [None, None]
    assert "Learning context: off" in md

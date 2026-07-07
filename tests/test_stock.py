"""Unit tests for app/stock.py — rotation and dedup behavior."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app import stock
from app.config import Settings
from app.models import Post, PostStatus


async def test_select_images_prefers_uncommitted(session: AsyncSession, tmp_path: Path) -> None:
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        (tmp_path / name).write_bytes(b"\xff\xd8\xff")  # minimal JPEG header

    settings = Settings(stock_images_dir=str(tmp_path))

    # a.jpg and b.jpg are committed (APPROVED) — c.jpg is free
    for name in ("a.jpg", "b.jpg"):
        session.add(
            Post(
                image_ref=name,
                caption_he="x",
                caption_en="x",
                visual_concept="x",
                rationale="x",
                status=PostStatus.APPROVED,
            )
        )
    await session.commit()

    selected = await stock.select_images(session, 2, settings)
    refs = [str(p.relative_to(tmp_path)) for p in selected]

    # c.jpg is the only uncommitted image — always picked first
    assert refs[0] == "c.jpg"
    # second slot cycles from the committed pool
    assert refs[1] in ("a.jpg", "b.jpg")


async def test_select_images_suggested_not_blocked(session: AsyncSession, tmp_path: Path) -> None:
    """SUGGESTED/REJECTED images are not treated as committed — available for reuse."""
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff")
    settings = Settings(stock_images_dir=str(tmp_path))
    session.add(
        Post(
            image_ref="a.jpg",
            caption_he="x",
            caption_en="x",
            visual_concept="x",
            rationale="x",
            status=PostStatus.SUGGESTED,
        )
    )
    await session.commit()
    selected = await stock.select_images(session, 1, settings)
    assert len(selected) == 1  # a.jpg is available despite being in a SUGGESTED post


async def test_select_images_cycles_when_pool_exhausted(
    session: AsyncSession, tmp_path: Path
) -> None:
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_bytes(b"\xff\xd8\xff")

    settings = Settings(stock_images_dir=str(tmp_path))

    # Requesting more images than the library holds — should cycle without error
    selected = await stock.select_images(session, 5, settings)
    assert len(selected) == 5
    assert all(p in [tmp_path / "a.jpg", tmp_path / "b.jpg"] for p in selected)

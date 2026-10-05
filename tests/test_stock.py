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

    # c.jpg is the only uncommitted image — and committed ones are never recycled
    assert refs == ["c.jpg"]


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


async def test_select_images_returns_fewer_when_pool_exhausted(
    session: AsyncSession, tmp_path: Path
) -> None:
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_bytes(b"\xff\xd8\xff")

    settings = Settings(stock_images_dir=str(tmp_path))

    # Requesting more images than the library holds — no duplicates, no error
    selected = await stock.select_images(session, 5, settings)
    assert sorted(selected) == [tmp_path / "a.jpg", tmp_path / "b.jpg"]
    assert await stock.low_stock_message(session, settings, 2, 5) is not None
    assert await stock.low_stock_message(session, settings, 5, 5) is None


async def test_banned_image_never_selected(session: AsyncSession, tmp_path: Path) -> None:
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_bytes(b"\xff\xd8\xff")
    settings = Settings(stock_images_dir=str(tmp_path))

    await stock.ban_image(session, "a.jpg")
    await stock.ban_image(session, "a.jpg")  # idempotent
    await session.commit()

    assert await stock.select_images(session, 5, settings) == [tmp_path / "b.jpg"]
    assert await stock.counts(session, settings) == {
        "unused": 1, "used": 0, "banned": 1, "total": 2
    }

    assert await stock.unban_image(session, "a.jpg") is True
    assert await stock.unban_image(session, "a.jpg") is False
    assert len(await stock.select_images(session, 5, settings)) == 2

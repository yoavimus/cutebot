"""Unit tests for app/stock.py — rotation and dedup behavior."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app import brands, stock
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
                brand_id=1,
                image_ref=name,
                caption_he="x",
                caption_en="x",
                visual_concept="x",
                rationale="x",
                status=PostStatus.APPROVED,
            )
        )
    await session.commit()

    selected = await stock.select_images(session, await brands.default(session), 2, settings)
    refs = [str(p.relative_to(tmp_path)) for p in selected]

    # c.jpg is the only uncommitted image — and committed ones are never recycled
    assert refs == ["c.jpg"]


async def test_select_images_suggested_not_blocked(session: AsyncSession, tmp_path: Path) -> None:
    """SUGGESTED/REJECTED images are not treated as committed — available for reuse."""
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff")
    settings = Settings(stock_images_dir=str(tmp_path))
    session.add(
        Post(
            brand_id=1,
            image_ref="a.jpg",
            caption_he="x",
            caption_en="x",
            visual_concept="x",
            rationale="x",
            status=PostStatus.SUGGESTED,
        )
    )
    await session.commit()
    selected = await stock.select_images(session, await brands.default(session), 1, settings)
    assert len(selected) == 1  # a.jpg is available despite being in a SUGGESTED post


async def test_select_images_returns_fewer_when_pool_exhausted(
    session: AsyncSession, tmp_path: Path
) -> None:
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_bytes(b"\xff\xd8\xff")

    settings = Settings(stock_images_dir=str(tmp_path))

    # Requesting more images than the library holds — no duplicates, no error
    selected = await stock.select_images(session, await brands.default(session), 5, settings)
    assert sorted(selected) == [tmp_path / "a.jpg", tmp_path / "b.jpg"]
    assert (
        await stock.low_stock_message(session, await brands.default(session), settings, 2, 5)
        is not None
    )
    assert (
        await stock.low_stock_message(session, await brands.default(session), settings, 5, 5)
        is None
    )


async def test_banned_image_never_selected(session: AsyncSession, tmp_path: Path) -> None:
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_bytes(b"\xff\xd8\xff")
    settings = Settings(stock_images_dir=str(tmp_path))

    await stock.ban_image(session, 1, "a.jpg")
    await stock.ban_image(session, 1, "a.jpg")  # idempotent
    await session.commit()

    assert await stock.select_images(session, await brands.default(session), 5, settings) == [
        tmp_path / "b.jpg"
    ]
    assert await stock.counts(session, await brands.default(session), settings) == {
        "unused": 1,
        "used": 0,
        "banned": 1,
        "total": 2,
    }

    assert await stock.unban_image(session, 1, "a.jpg") is True
    assert await stock.unban_image(session, 1, "a.jpg") is False
    assert len(await stock.select_images(session, await brands.default(session), 5, settings)) == 2


async def test_pending_retry_and_kept_hold_their_image(
    session: AsyncSession, tmp_path: Path
) -> None:
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        (tmp_path / name).write_bytes(b"\xff\xd8\xff")
    settings = Settings(stock_images_dir=str(tmp_path))

    def post(ref: str, status: PostStatus, retry_of: int | None = None) -> Post:
        return Post(
            brand_id=1,
            image_ref=ref,
            caption_he="x",
            caption_en="x",
            visual_concept="x",
            status=status,
            retry_of=retry_of,
        )

    almost = post("a.jpg", PostStatus.REJECTED)
    session.add_all([almost, post("b.jpg", PostStatus.KEPT)])
    await session.commit()
    session.add(post("a.jpg", PostStatus.SUGGESTED, retry_of=almost.id))
    await session.commit()
    refs = {
        str(p.relative_to(tmp_path))
        for p in await stock.select_images(session, await brands.default(session), 3, settings)
    }
    assert refs == {"c.jpg"}


async def test_register_dedupes_and_backfill_marks_manual(
    session: AsyncSession, tmp_path: Path
) -> None:
    settings = Settings(stock_images_dir=str(tmp_path))
    brand = await brands.default(session)
    (tmp_path / "old.jpg").write_bytes(b"\xff\xd8\xff-old")  # on the volume, no row
    assert await stock.backfill_manual(session, brand, settings) == 1
    assert await stock.backfill_manual(session, brand, settings) == 0
    row = await session.get(stock.StockImage, (brand.id, "old.jpg"))
    assert row is not None and row.source == "manual"

    (tmp_path / "new.jpg").write_bytes(b"\xff\xd8\xff-old")  # same bytes as old.jpg
    assert await stock.register(session, brand, tmp_path / "new.jpg", "telegram", "f") is None
    assert not (tmp_path / "new.jpg").exists()

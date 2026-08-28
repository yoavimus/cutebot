"""Tests for app/publishers/instagram.py — Graph mocks, validation, idempotency (M7.5).

The Graph HTTP seam (_graph_post/_graph_get) is monkeypatched; no network is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Post, PostStatus
from app.publishers import instagram


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        stock_images_dir=str(tmp_path),
        instagram_access_token="tok",
        instagram_ig_user_id="ig-user",
        public_base_url="https://example.test",
    )


def _make_post(tmp_path: Path, name: str = "a.jpg", size: tuple[int, int] = (1080, 1080)) -> Post:
    Image.new("RGB", size, color="red").save(tmp_path / name, format="JPEG")
    return Post(
        image_ref=name,
        caption_he="שלום",
        caption_en="hello",
        visual_concept="v",
        rationale="r",
        status=PostStatus.APPROVED,
    )


async def test_publish_happy_path(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    post = _make_post(tmp_path)
    session.add(post)
    await session.commit()

    calls: list[str] = []

    async def fake_graph_post(path: str, data: dict[str, Any], s: Settings) -> dict[str, Any]:
        calls.append(path)
        if path.endswith("/media"):
            assert data["image_url"] == "https://example.test/media/a.jpg"
            assert "שלום" in data["caption"] and "hello" in data["caption"]
            return {"id": "container-1"}
        assert data["creation_id"] == "container-1"
        return {"id": "media-1"}

    async def fake_graph_get(path: str, params: dict[str, Any], s: Settings) -> dict[str, Any]:
        assert path == "container-1"
        return {"status_code": "FINISHED"}

    monkeypatch.setattr(instagram, "_graph_post", fake_graph_post)
    monkeypatch.setattr(instagram, "_graph_get", fake_graph_get)
    monkeypatch.setattr(instagram, "get_settings", lambda: settings)

    result = await instagram.InstagramPublisher().publish(session, post)

    assert result.ok is True
    assert result.detail == "media-1"
    assert calls == ["ig-user/media", "ig-user/media_publish"]
    await session.refresh(post)
    assert post.ig_container_id == "container-1"
    assert post.ig_media_id == "media-1"


async def test_publish_graph_error_fails_post_without_crashing(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    post = _make_post(tmp_path)
    session.add(post)
    await session.commit()

    async def fake_graph_post(path: str, data: dict[str, Any], s: Settings) -> dict[str, Any]:
        return {"error": {"message": "Invalid parameter", "code": 100}}

    monkeypatch.setattr(instagram, "_graph_post", fake_graph_post)
    monkeypatch.setattr(instagram, "get_settings", lambda: settings)

    result = await instagram.InstagramPublisher().publish(session, post)

    assert result.ok is False
    assert "Invalid parameter" in result.detail
    await session.refresh(post)
    assert post.ig_container_id is None


async def test_publish_rejects_out_of_spec_image(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    post = _make_post(tmp_path, size=(100, 100))  # width below IG's 320px floor
    session.add(post)
    await session.commit()
    monkeypatch.setattr(instagram, "get_settings", lambda: settings)

    result = await instagram.InstagramPublisher().publish(session, post)

    assert result.ok is False
    assert "width" in result.detail


async def test_publish_resumes_from_existing_container_no_recreate(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, tmp_path: Path
) -> None:
    """A retry after a container was already created must not create a second one."""
    settings = _settings(tmp_path)
    post = _make_post(tmp_path)
    post.ig_container_id = "container-existing"
    session.add(post)
    await session.commit()

    post_calls: list[str] = []

    async def fake_graph_post(path: str, data: dict[str, Any], s: Settings) -> dict[str, Any]:
        post_calls.append(path)
        assert path.endswith("/media_publish")
        return {"id": "media-2"}

    async def fake_graph_get(path: str, params: dict[str, Any], s: Settings) -> dict[str, Any]:
        assert path == "container-existing"
        return {"status_code": "FINISHED"}

    monkeypatch.setattr(instagram, "_graph_post", fake_graph_post)
    monkeypatch.setattr(instagram, "_graph_get", fake_graph_get)
    monkeypatch.setattr(instagram, "get_settings", lambda: settings)

    result = await instagram.InstagramPublisher().publish(session, post)

    assert result.ok is True
    assert post_calls == ["ig-user/media_publish"]  # no container create call


def test_publishing_switch_selects_stub_vs_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """PUBLISHING_ENABLED gates the real adapter even when creds are present."""
    from app.publishers import base
    from app.publishers.instagram import InstagramPublisher as RealIG

    creds = dict(instagram_access_token="tok", instagram_ig_user_id="ig-user")

    monkeypatch.setattr(base, "get_settings", lambda: Settings(publishing_enabled=True, **creds))
    assert isinstance(base.get_publishers()[0], RealIG)  # live → real

    monkeypatch.setattr(base, "get_settings", lambda: Settings(publishing_enabled=False, **creds))
    assert isinstance(base.get_publishers()[0], base.InstagramPublisher)  # kill-switch → stub

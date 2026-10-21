"""M10.3 — tokens + cost on posts/batches, the /status cost line (offline)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import litellm
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app import brands, llm, stock
from app.config import Settings, get_settings
from app.models import Batch, Brand
from app.notifier.telegram import _build_status_summary
from app.pipeline import generate
from app.schemas import PostSuggestion

_PRICES = {"input_cost_per_token": 1e-5, "output_cost_per_token": 5e-5}


@pytest.fixture(autouse=True)
def _stock(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_select(
        session: AsyncSession, brand: Brand, n: int, settings: Settings
    ) -> list[Path]:
        return [Path(f"{i}.jpg") for i in range(n)]

    monkeypatch.setattr(stock, "select_images", fake_select)
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "anthropic/claude-opus-5-5")
    get_settings.cache_clear()
    monkeypatch.setitem(litellm.model_cost, "claude-opus-5-5", _PRICES)


def _caption(tokens: tuple[int | None, int | None]) -> object:
    async def fake(*a: object, **k: object) -> PostSuggestion:
        return PostSuggestion(
            caption_he="ע",
            caption_en="e",
            visual_concept="v",
            rationale="r",
            tokens_in=tokens[0],
            tokens_out=tokens[1],
        )

    return fake


@pytest.fixture(autouse=True)
def _reset_settings() -> object:
    yield
    get_settings.cache_clear()


def test_cost_usd_prices_known_models_only() -> None:
    assert llm.cost_usd("anthropic/claude-opus-5-5@high", 1000, 100) == pytest.approx(0.015)
    assert llm.cost_usd("anthropic/claude-opus-5-5", None, 100) is None
    assert llm.cost_usd("anthropic/no-such-model", 1000, 100) is None


async def test_usage_is_stored_and_summed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm, "caption_image", _caption((1000, 100)))
    brand = await brands.default(session)
    posts = await generate.generate_batch(session, brand, n=2)
    assert [(p.tokens_in, p.tokens_out, p.cost_usd) for p in posts] == [
        (1000, 100, Decimal("0.015000"))
    ] * 2
    batch = await session.get(Batch, posts[0].batch_id)
    assert batch is not None and batch.cost_usd == Decimal("0.030000")

    summary = await _build_status_summary(session, brand, Settings())
    assert "cost: $0.03 over 2 posts (last 30d)" in summary


async def test_stub_batch_has_null_cost(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm, "caption_image", _caption((None, None)))
    brand = await brands.default(session)
    (post,) = await generate.generate_batch(session, brand, n=1)
    assert (post.tokens_in, post.cost_usd) == (None, None)
    batch = await session.get(Batch, post.batch_id)
    assert batch is not None and batch.cost_usd is None
    assert "no priced posts" in await _build_status_summary(session, brand, Settings())

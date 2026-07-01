"""Live integration test — requires ANTHROPIC_API_KEY in the environment.

Run with:  pytest -m live
CI stays keyless; this test skips automatically when no key is present.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import get_settings
from app.llm import _has_provider_key, caption_image

pytestmark = pytest.mark.live


async def test_caption_image_returns_bilingual_output() -> None:
    settings = get_settings()
    if not _has_provider_key():
        pytest.skip("No LLM provider key configured")

    stock_dir = Path(settings.stock_images_dir)
    images = sorted(
        p for p in stock_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    assert images, f"No stock images found in {stock_dir}"

    result = await caption_image(
        "Test brand. Write warm, casual posts in Hebrew (primary) and English.", images[0], settings
    )

    assert result.caption_he and re.search(
        r"[֐-׿]", result.caption_he
    ), "caption_he must contain Hebrew characters"
    assert result.caption_en, "caption_en must be non-empty"
    assert result.visual_concept, "visual_concept must be non-empty"
    assert result.rationale, "rationale must be non-empty"

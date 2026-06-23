"""Load brand guidelines. Injected verbatim into the generation prompt."""

from __future__ import annotations

from pathlib import Path

from app.config import get_settings

_DEFAULT_BRAND = """\
name: Demo Brand
voice:
  - Friendly, concise, a little playful.
do_not:
  - No exclamation-point spam.
pillars:
  - Behind-the-scenes moments.
"""


def load_brand(path: str | None = None) -> str:
    """Return the brand guidelines as raw text.

    Falls back to a small built-in default so the pipeline runs out of the box
    before a real ``brand.yaml`` exists.
    """
    brand_path = Path(path or get_settings().brand_file)
    if brand_path.exists():
        return brand_path.read_text(encoding="utf-8")
    return _DEFAULT_BRAND

"""Bilingual + disclaimer caption composition — the single source of this guarantee.

Both the Telegram notifier and the publishers call ``render_full_caption`` so the
"every post is bilingual and carries the disclaimer" rule lives in exactly one place.
See PRODUCT_SPEC §3, §7.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.models import Post

_CAPTION_FIELDS = {"he": "caption_he", "en": "caption_en"}


def render_full_caption(post: Post, settings: Settings) -> str:
    """Compose the publishable caption: primary, then secondaries, then the disclaimer."""
    languages = [settings.primary_language, *settings.secondary_languages_list]
    captions = [
        getattr(post, _CAPTION_FIELDS[lang]) for lang in languages if lang in _CAPTION_FIELDS
    ]
    return "\n\n".join([*captions, settings.post_disclaimer])


def image_path(post: Post, settings: Settings) -> Path:
    """Resolve the post's ``image_ref`` against the configured stock directory."""
    return Path(settings.stock_images_dir) / post.image_ref

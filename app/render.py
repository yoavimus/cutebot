"""Bilingual + disclaimer caption composition — the single source of this guarantee.

Both the Telegram notifier and the publishers call ``render_full_caption`` so the
"every post is bilingual and carries the disclaimer" rule lives in exactly one place.
See PRODUCT_SPEC §3, §7.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.config import Settings
from app.models import Post

_CAPTION_FIELDS = {"he": "caption_he", "en": "caption_en"}

# IG feed image spec: aspect 4:5–1.91, width ≤ 1440, height ≤ 1800.
# https://developers.facebook.com/docs/instagram-platform/content-publishing
_IG_MIN_ASPECT, _IG_MAX_ASPECT = 4 / 5, 1.91
_IG_MAX_W, _IG_MAX_H = 1440, 1800


def prepare_ig_image(img: Image.Image) -> Image.Image:
    """Make an image IG-feed-compliant: pad an off-spec aspect to the nearest valid
    ratio with white bars (whole image kept), then fit inside 1440×1800.

    Serving through this means posts never fail on width/aspect — the only thing left
    for the publisher to reject is an image so small it stays under 320px after fitting.
    """
    img = img.convert("RGB")
    w, h = img.size
    aspect = w / h
    target = min(max(aspect, _IG_MIN_ASPECT), _IG_MAX_ASPECT)
    if target > aspect:  # too tall — widen the canvas
        canvas_w, canvas_h = round(h * target), h
    elif target < aspect:  # too wide — heighten the canvas
        canvas_w, canvas_h = w, round(w / target)
    else:
        canvas_w, canvas_h = w, h
    if (canvas_w, canvas_h) != (w, h):
        canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
        canvas.paste(img, ((canvas_w - w) // 2, (canvas_h - h) // 2))
        img = canvas
    img.thumbnail((_IG_MAX_W, _IG_MAX_H))  # shrinks only, preserves aspect
    return img


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


def resolve_media_path(image_ref: str, settings: Settings) -> Path | None:
    """Resolve ``image_ref`` under the stock directory for ``GET /media/{ref}``.

    Returns None if the ref escapes the stock directory (path traversal) or doesn't
    exist — callers should 404 either case without distinguishing them.
    """
    base = Path(settings.stock_images_dir).resolve()
    candidate = (base / image_ref).resolve()
    if not candidate.is_relative_to(base) or not candidate.is_file():
        return None
    return candidate

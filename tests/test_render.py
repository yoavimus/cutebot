"""prepare_ig_image — makes any source IG-feed-compliant (pad off-spec aspect, fit size)."""

from __future__ import annotations

from PIL import Image

from app.render import prepare_ig_image

_MIN_A, _MAX_A = 4 / 5, 1.91


def _aspect_ok(img: Image.Image) -> bool:
    w, h = img.size
    a = w / h
    return _MIN_A - 1e-3 <= a <= _MAX_A + 1e-3 and w <= 1440 and h <= 1800


def test_too_tall_is_padded_into_spec() -> None:
    out = prepare_ig_image(Image.new("RGB", (1200, 1600)))  # 0.75, below 4:5
    assert _aspect_ok(out)


def test_too_wide_is_padded_into_spec() -> None:
    out = prepare_ig_image(Image.new("RGB", (4000, 1000)))  # 4.0, above 1.91
    assert _aspect_ok(out)


def test_in_spec_only_downscaled_not_padded() -> None:
    out = prepare_ig_image(Image.new("RGB", (4000, 3000)))  # 1.333, in range
    assert _aspect_ok(out)
    assert abs(out.size[0] / out.size[1] - 4000 / 3000) < 1e-3  # aspect unchanged


def test_small_in_spec_image_not_upscaled() -> None:
    out = prepare_ig_image(Image.new("RGB", (200, 200)))
    assert out.size == (200, 200)  # never enlarge; publisher rejects <320 separately

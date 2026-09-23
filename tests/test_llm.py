"""Offline unit tests for the LLM seam's pure helpers."""

from __future__ import annotations

from app.llm import _unwrap_json_envelope

_FLAT = {"caption_he": "א", "caption_en": "a", "visual_concept": "v", "rationale": "r"}


def test_unwraps_json_envelope() -> None:
    assert _unwrap_json_envelope({"json": _FLAT}) == _FLAT


def test_leaves_flat_object_untouched() -> None:
    assert _unwrap_json_envelope(_FLAT) == _FLAT


def test_does_not_unwrap_when_json_is_not_a_dict() -> None:
    assert _unwrap_json_envelope({"json": "oops"}) == {"json": "oops"}


def test_does_not_unwrap_when_other_keys_present() -> None:
    d = {"json": _FLAT, "extra": 1}
    assert _unwrap_json_envelope(d) == d  # a real field literally named "json" stays put

"""Pydantic schemas — the structured contract for LLM output and API responses.

Structured data over parsing: the generation agent returns ``PostSuggestion`` objects,
never free text. See DEV_GUIDELINES.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PostSuggestion(BaseModel):
    """One generated post idea. This is exactly what the LLM must return per item."""

    caption: str = Field(description="The post caption, written in the brand's voice.")
    visual_concept: str = Field(description="A short description of the matching image/video.")
    rationale: str = Field(default="", description="Why this fits the brand (for the reviewer).")


class GenerationResult(BaseModel):
    """Wrapper the LLM returns: a list of suggestions."""

    posts: list[PostSuggestion]


class PostOut(BaseModel):
    """API representation of a stored post."""

    id: int
    caption: str
    visual_concept: str
    rationale: str
    status: str
    queue_position: int | None

    model_config = {"from_attributes": True}

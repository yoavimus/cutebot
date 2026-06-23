"""Pydantic schemas — the structured contract for LLM output and API responses.

Structured data over parsing: the generation agent returns ``PostSuggestion`` objects,
never free text. See DEV_GUIDELINES.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PostSuggestion(BaseModel):
    """One generated post idea, grounded in a specific image. Exactly what the LLM must return."""

    caption_he: str = Field(description="The Hebrew (primary) caption, in the brand's voice.")
    caption_en: str = Field(description="The English caption, in the brand's voice.")
    visual_concept: str = Field(description="A short description of the image the caption matches.")
    rationale: str = Field(default="", description="Why this fits the brand (for the reviewer).")


class PostOut(BaseModel):
    """API representation of a stored post."""

    id: int
    image_ref: str
    caption_he: str
    caption_en: str
    visual_concept: str
    rationale: str
    status: str
    queue_position: int | None

    model_config = {"from_attributes": True}

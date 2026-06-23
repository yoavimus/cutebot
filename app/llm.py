"""The single LLM seam. All model access goes through here (LiteLLM, Claude by default).

Pipeline code must never import an LLM SDK directly — call ``generate_suggestions``.
When no provider key is configured the function returns deterministic stub suggestions
so the skeleton runs (and tests pass) fully offline.
"""

from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.schemas import GenerationResult, PostSuggestion

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are CuteBot, an expert social-media copywriter for a single brand.
Write posts that sound exactly like the brand's own voice — never generic or corporate.
You must follow the brand's hard rules. Return ONLY valid JSON.
"""

_USER_TEMPLATE = """\
Here are the brand guidelines (the source of truth for voice, rules, and themes):

<brand_guidelines>
{brand}
</brand_guidelines>

Generate {n} distinct post suggestions. Rotate across the brand's content pillars and
vary the angle so the batch doesn't feel repetitive.

Return a JSON object of this exact shape:
{{"posts": [{{"caption": "...", "visual_concept": "...", "rationale": "..."}}]}}

- caption: the post text, in the brand's voice, respecting every hard rule.
- visual_concept: one sentence describing the matching image or video.
- rationale: one sentence on why this fits the brand (for the human reviewer).
"""


def _provider_key() -> str | None:
    """The API key for the configured provider, or None if not set.

    LiteLLM reads keys from ``os.environ`` only — pydantic-settings loads them into the
    ``Settings`` object, not the environment — so we pass the key explicitly to
    ``acompletion`` rather than relying on env lookup.
    """
    s = get_settings()
    model = s.default_llm_model
    if model.startswith("anthropic/"):
        return s.anthropic_api_key or None
    if model.startswith("openai/") or model.startswith("gpt"):
        return s.openai_api_key or None
    return None


def _has_provider_key() -> bool:
    s = get_settings()
    model = s.default_llm_model
    if model.startswith(("anthropic/", "openai/", "gpt")):
        return _provider_key() is not None
    # Unknown provider — assume the env is configured for it.
    return True


def _stub_suggestions(n: int) -> list[PostSuggestion]:
    return [
        PostSuggestion(
            caption=f"[stub caption #{i + 1}] set ANTHROPIC_API_KEY to generate real posts.",
            visual_concept=f"[stub visual #{i + 1}] a cozy product shot in warm light.",
            rationale="Offline stub — no LLM provider key configured.",
        )
        for i in range(n)
    ]


async def generate_suggestions(brand: str, n: int) -> list[PostSuggestion]:
    """Generate ``n`` on-brand post suggestions via the configured Claude/LiteLLM model."""
    if not _has_provider_key():
        logger.warning("No LLM provider key configured — returning %d stub suggestions.", n)
        return _stub_suggestions(n)

    # Imported lazily so the package imports cleanly without litellm installed in
    # minimal environments, and so the offline path never touches the network.
    import litellm

    settings = get_settings()
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _USER_TEMPLATE.format(brand=brand, n=n)},
    ]
    response = await litellm.acompletion(
        model=settings.default_llm_model,
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=2000,
        # LiteLLM only reads keys from os.environ; pass explicitly since pydantic-settings
        # loads them into Settings, not the environment.
        api_key=_provider_key(),
    )
    content = response["choices"][0]["message"]["content"]
    data = json.loads(content)
    result = GenerationResult.model_validate(data)
    return result.posts[:n]

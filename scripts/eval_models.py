"""Model bake-off for the Hebrew quality gate (DEV_GUIDELINES "Model decision").

Runs the same stock images x the same brand file through several candidate models via
the app's own LLM seam (app/llm.py), and writes the captions side-by-side to one
Markdown file for native-speaker review.

Usage (from the repo root, venv active, keys in .env):

    python -m scripts.eval_models                          # defaults below
    python -m scripts.eval_models --models anthropic/claude-sonnet-5-5,openai/gpt-6-sol
    python -m scripts.eval_models --images 5 --out eval/eval_results.md

A candidate is ``<litellm id>[@<reasoning effort>]`` — ``openai/gpt-6.1-sol@high``
runs Sol at high reasoning; no suffix = provider default. (Effort applies to OpenAI
models only.) Each model header carries elapsed time and the cost of that one post
(provider usage report x LiteLLM's bundled price table); the file header totals
cost per candidate. Candidates whose provider key is missing produce the offline stub —
obvious in the output, not an error.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from pathlib import Path

from app import llm, stock
from app.brand import load_brand
from app.config import get_settings

# Round 2 (2026-10): current Anthropic + OpenAI lineups (adjust freely via --models).
# Round 1 (sonnet-4-6 / opus-4-8 / gpt-5.1) picked gpt-5.1 — the runtime default.
DEFAULT_MODELS = [
    "anthropic/claude-sonnet-5-5",
    "anthropic/claude-opus-5-5",
    "openai/gpt-6.1-sol@medium",
    "openai/gpt-6.1-sol@xhigh",
]

def post_cost(model: str, tokens_in: int | None, tokens_out: int | None) -> float | None:
    """Cost of one call from LiteLLM's bundled price table (None if the id is unknown)."""
    import litellm

    price = litellm.model_cost.get(model.split("@")[0].split("/", 1)[-1])
    if not price or tokens_in is None or tokens_out is None:
        return None
    return tokens_in * price["input_cost_per_token"] + tokens_out * price["output_cost_per_token"]


async def run(models: list[str], n_images: int, out: str) -> None:
    settings = get_settings()
    brand = load_brand()
    images = stock.list_images(settings)
    if not images:
        raise SystemExit(f"No stock images in {settings.stock_images_dir!r} — nothing to caption.")
    sample = random.sample(images, min(n_images, len(images)))

    lines = [
        "# Model eval — Hebrew quality gate",
        "",
        f"Brand file: `{settings.brand_file}` · images: {len(sample)} · models: "
        + ", ".join(f"`{m}`" for m in models),
        "",
        "Review guide: native-quality Hebrew (not translated), brand voice, hard-rule",
        "adherence (signature line, no emoji, no serious marketing tone).",
        "",
    ]

    totals: dict[str, float] = dict.fromkeys(models, 0.0)
    unpriced: set[str] = set()
    for image in sample:
        lines += [f"## {image.name}", "", f"![{image.name}]({image.as_posix()})", ""]
        for model in models:
            model_id, _, effort = model.partition("@")
            s = settings.model_copy(
                update={"default_llm_model": model_id, "llm_reasoning_effort": effort}
            )
            t0 = time.monotonic()
            try:
                sug = await llm.caption_image(brand, image, s)
                elapsed = time.monotonic() - t0
                cost = post_cost(model, sug.tokens_in, sug.tokens_out)
                if cost is None:
                    unpriced.add(model)
                    cost_str = "cost ?"
                else:
                    totals[model] += cost
                    cost_str = f"${cost:.4f} · {sug.tokens_in} in / {sug.tokens_out} out"
                lines += [
                    f"### {model}  ({elapsed:.1f}s · {cost_str})",
                    "",
                    "**HE:**",
                    "",
                    sug.caption_he,
                    "",
                    "**EN:**",
                    "",
                    sug.caption_en,
                    "",
                    f"*rationale: {sug.rationale}*",
                    "",
                ]
            except Exception as exc:  # noqa: BLE001 — keep the sweep going per candidate
                lines += [f"### {model}", "", f"**ERROR:** {exc}", ""]
            print(f"{image.name} × {model}: done")

    # Cost-per-model header line (parsed by eval_to_html into the page header).
    per_model = ", ".join(
        f"`{m}` ${totals[m]:.3f}" + (" (partial)" if m in unpriced else "") for m in models
    )
    lines.insert(3, f"Cost for {len(sample)} posts: {per_model}")
    lines.insert(4, "")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {out} — review side-by-side and record the decision in DEV_GUIDELINES.md.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--models", default=",".join(DEFAULT_MODELS), help="comma-separated LiteLLM ids")
    p.add_argument("--images", type=int, default=4, help="how many stock images to sample")
    p.add_argument("--out", default="eval/eval_results.md", help="output Markdown file")
    args = p.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    asyncio.run(run(models, args.images, args.out))


if __name__ == "__main__":
    main()

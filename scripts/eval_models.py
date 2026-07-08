"""Model bake-off for the Hebrew quality gate (DEV_GUIDELINES "Model decision").

Runs the same stock images x the same brand file through several candidate models via
the app's own LLM seam (app/llm.py), and writes the captions side-by-side to one
Markdown file for native-speaker review.

Usage (from the repo root, venv active, keys in .env):

    python -m scripts.eval_models                          # defaults below
    python -m scripts.eval_models --models anthropic/claude-sonnet-4-6,openai/gpt-5.1
    python -m scripts.eval_models --images 5 --out eval_results.md

Candidates whose provider key is missing produce the offline stub — obvious in the
output, not an error — so round 1 can run before Anthropic credits arrive.
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

# Same-tier candidates across providers (adjust freely via --models):
# Anthropic Sonnet/Opus tiers + the OpenAI equivalent. Add Gemini in round 2,
# e.g. gemini/gemini-<current-tier-id>.
DEFAULT_MODELS = [
    "anthropic/claude-sonnet-4-6",  # current runtime default
    "anthropic/claude-opus-4-8",
    "openai/gpt-5.1",
]


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

    for image in sample:
        lines += [f"## {image.name}", "", f"![{image.name}]({image.as_posix()})", ""]
        for model in models:
            s = settings.model_copy(update={"default_llm_model": model})
            t0 = time.monotonic()
            try:
                sug = await llm.caption_image(brand, image, s)
                elapsed = time.monotonic() - t0
                lines += [
                    f"### {model}  ({elapsed:.1f}s)",
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

    Path(out).write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {out} — review side-by-side and record the decision in DEV_GUIDELINES.md.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--models", default=",".join(DEFAULT_MODELS), help="comma-separated LiteLLM ids")
    p.add_argument("--images", type=int, default=4, help="how many stock images to sample")
    p.add_argument("--out", default="eval_results.md", help="output Markdown file")
    args = p.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    asyncio.run(run(models, args.images, args.out))


if __name__ == "__main__":
    main()

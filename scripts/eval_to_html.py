"""Render an ``eval_results.md`` (from scripts.eval_models) as a side-by-side HTML page
with each stock image beside every model's caption, for native-speaker review.

Local file, no upload — images load by relative path from the repo root, so open the
output from the repo root (where ``stock/`` lives).

    python -m scripts.eval_to_html                     # eval/eval_results.md -> eval/eval_results.html
    python -m scripts.eval_to_html --in x.md --out x.html
"""
# ruff: noqa: E501 — embedded CSS/HTML template lines are intentionally long

from __future__ import annotations

import argparse
import html
import os
import re
from pathlib import Path

from app.stock import load_image_b64


def parse(md: str) -> list[dict]:
    """Parse the rigid eval_results.md structure into image → models records."""
    images: list[dict] = []
    cur_img: dict | None = None
    cur_model: dict | None = None
    field: str | None = None  # "he" | "en"

    def flush_model() -> None:
        nonlocal cur_model
        if cur_model and cur_img:
            cur_model["he"] = cur_model["he"].strip()
            cur_model["en"] = cur_model["en"].strip()
            cur_img["models"].append(cur_model)
        cur_model = None

    for line in md.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            flush_model()
            if cur_img:
                images.append(cur_img)
            cur_img = {"name": line[3:].strip(), "src": None, "models": []}
            field = None
        elif line.startswith("!["):
            m = re.search(r"\]\(([^)]+)\)", line)
            if m and cur_img and cur_img["src"] is None:
                cur_img["src"] = m.group(1)
        elif line.startswith("### "):
            flush_model()
            rest = line[4:].strip()
            mt = re.match(r"(.*?)\s*\(([^)]*)\)\s*$", rest)
            name, t = (mt.group(1).strip(), mt.group(2)) if mt else (rest, "")
            cur_model = {"name": name, "time": t, "he": "", "en": "", "rationale": ""}
            field = None
        elif line.strip() == "**HE:**":
            field = "he"
        elif line.strip() == "**EN:**":
            field = "en"
        elif line.startswith("**ERROR:**") and cur_model:
            cur_model["he"] = cur_model["en"] = line[len("**ERROR:**") :].strip()
            field = None
        elif line.startswith("*rationale:") and cur_model:
            cur_model["rationale"] = line.strip().strip("*")[len("rationale:") :].strip()
            field = None
        elif cur_model and field in ("he", "en"):
            cur_model[field] += line + "\n"

    flush_model()
    if cur_img:
        images.append(cur_img)
    return images


def _para(text: str) -> str:
    """Escape + turn blank-line-separated blocks into <p>."""
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    return "".join(f"<p>{html.escape(b).replace(chr(10), '<br>')}</p>" for b in blocks) or "<p>—</p>"


_CSS = """
:root{--bg:#faf9f7;--card:#fff;--ink:#1a1a1a;--muted:#6b6b6b;--line:#e6e3de;--accent:#7c5cff}
@media(prefers-color-scheme:dark){:root{--bg:#16151a;--card:#201f26;--ink:#ececef;--muted:#9a97a3;--line:#322f3a;--accent:#a78bfa}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
header{padding:24px 16px;max-width:1560px;margin:0 auto}
h1{font-size:1.4rem;margin:0 0 4px}
.sub{color:var(--muted);font-size:.9rem}
main{max-width:1560px;margin:0 auto;padding:0 16px 60px}
.imgcard{background:var(--card);border:1px solid var(--line);border-radius:14px;margin:22px 0;overflow:hidden}
.imgcard>h2{font-size:.95rem;color:var(--muted);font-weight:600;margin:0;padding:12px 16px;border-bottom:1px solid var(--line);font-family:ui-monospace,Menlo,monospace}
.body{display:grid;grid-template-columns:minmax(240px,340px) 1fr;gap:0}
.photo{padding:16px;border-inline-end:1px solid var(--line)}
.photo img{width:100%;border-radius:10px;display:block}
.cols{display:grid;grid-template-columns:repeat(var(--n),minmax(220px,1fr));overflow-x:auto}
.model{padding:16px;border-inline-start:1px solid var(--line)}
.model:first-child{border-inline-start:0}
.mhead{display:flex;flex-direction:column;align-items:flex-start;gap:6px;margin-bottom:10px;min-width:0}
.mname{font-weight:700;font-size:.82rem;overflow-wrap:anywhere}
.time{font-family:ui-monospace,Menlo,monospace;font-size:.72rem;color:#fff;background:var(--accent);padding:2px 7px;border-radius:999px;max-width:100%;overflow-wrap:anywhere}
.lbl{font-size:.68rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:12px 0 2px}
.he{direction:rtl;text-align:right}
.he p,.en p{margin:.3em 0}
.en{font-size:.92rem;color:var(--muted)}
.rat{margin-top:10px;padding-top:8px;border-top:1px dashed var(--line);font-size:.8rem;color:var(--muted);font-style:italic}
@media(max-width:760px){.body{grid-template-columns:1fr}.photo{border-inline-end:0;border-bottom:1px solid var(--line)}}
"""


def _img_src(img: dict, embed: bool, out_dir: Path) -> str:
    """The <img src> value: a self-contained data URI when embedding (downscaled, so the
    file stays small and sendable), else the stock path made relative to the output file."""
    src = img["src"] or ""
    if not src:
        return ""
    if not embed:
        return html.escape(os.path.relpath(src, out_dir))
    mime, b64 = load_image_b64(Path(src))  # downscales + JPEG-encodes; ~a few hundred KB
    return f"data:{mime};base64,{b64}"


def cost_line(md: str) -> str:
    """The ``Cost for N posts: ...`` header line written by eval_models, markdown stripped."""
    for line in md.splitlines():
        if line.startswith("Cost for "):
            return line.replace("`", "")
    return ""


def render(
    images: list[dict], title: str, embed: bool = False, out_dir: Path = Path("."), cost: str = ""
) -> str:
    cards = []
    for img in images:
        cols = []
        for m in img["models"]:
            cols.append(
                f'<div class="model"><div class="mhead"><span class="mname">{html.escape(m["name"])}</span>'
                + (f'<span class="time">{html.escape(m["time"])}</span>' if m["time"] else "")
                + '</div><div class="lbl">Hebrew</div>'
                + f'<div class="he">{_para(m["he"])}</div>'
                + '<div class="lbl">English</div>'
                + f'<div class="en">{_para(m["en"])}</div>'
                + (f'<div class="rat">{html.escape(m["rationale"])}</div>' if m["rationale"] else "")
                + "</div>"
            )
        src = _img_src(img, embed, out_dir)
        cards.append(
            f'<section class="imgcard"><h2>{html.escape(img["name"])}</h2>'
            f'<div class="body"><div class="photo"><img loading="lazy" src="{src}" alt=""></div>'
            f'<div class="cols" style="--n:{len(cols)}">{"".join(cols)}</div></div></section>'
        )
    return (
        f"<!doctype html><html lang=he><head><meta charset=utf-8>"
        f'<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>"
        f"<header><h1>{html.escape(title)}</h1>"
        f'<div class="sub">Native-Hebrew review · Hebrew is the quality gate, English is secondary · '
        f"{len(images)} images × {len(images[0]['models']) if images else 0} models</div>"
        + (f'<div class="sub">{html.escape(cost)}</div>' if cost else "")
        + "</header>"
        f"<main>{''.join(cards)}</main></body></html>"
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--in", dest="src", default="eval/eval_results.md")
    p.add_argument("--out", default="eval/eval_results.html")
    p.add_argument(
        "--embed",
        action="store_true",
        help="inline downscaled images as data URIs → one self-contained, sendable file",
    )
    args = p.parse_args()
    md = Path(args.src).read_text(encoding="utf-8")
    images = parse(md)
    if not images:
        raise SystemExit(f"No image sections parsed from {args.src!r}.")
    html_out = render(
        images,
        "Model eval — Hebrew quality gate",
        embed=args.embed,
        out_dir=Path(args.out).parent,
        cost=cost_line(md),
    )
    Path(args.out).write_text(html_out, encoding="utf-8")
    if args.embed:
        print(f"Wrote {args.out} — self-contained, send it anywhere.")
    else:
        print(f"Wrote {args.out} — images load from stock/ by relative path.")


if __name__ == "__main__":
    main()

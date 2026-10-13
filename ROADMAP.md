# CuteBot — Build Roadmap

> **Sequencing** doc: what shipped, what's next, what's parked. `PRODUCT_SPEC.md` is the
> source of truth for *what* CuteBot is — when scope changes, change the spec first,
> then resequence here. Active milestone plans live in `docs/plans/<ticket>.md` and move
> to `docs/archive/` when shipped.

## Locked decisions

- **Captioning = vision.** Generation passes the actual stock image to the model
  (multimodal); the caption is grounded in what it sees, not in a filename. (PRODUCT_SPEC §3.)
- **Bilingual (Hebrew-first) + mandatory disclaimer.** `caption_he` / `caption_en`,
  Hebrew quality is a hard gate; the disclaimer is appended **in code** at review and
  publish. (PRODUCT_SPEC §1, §3, §7.)
- **Runtime model is picked by eval, not opinion.** `scripts/eval_models.py` bake-off,
  native Hebrew review by the owner, re-run after each provider release. Current:
  Claude Opus 5.5 (DEV_GUIDELINES "Model decision" has the history).

## Shipped

Each milestone's full plan + checklist is archived under `docs/archive/`; this table is
the index. Details of what each one changed live in those plans and in git.

| Milestone | What | Shipped | Plan |
|-----------|------|---------|------|
| M0 — Image-first generation | `Post.image_ref` + bilingual captions, Alembic, `app/stock.py`, disclaimer in code, vision captioning, Telegram photo cards | 2026-07 | `docs/archive/M0_PLAN.md` |
| M1 — Real generation, validated | live API, Hebrew quality gate, `brand.*` file, retries/timeouts, image rotation | 2026-07 | `docs/archive/M1_PLAN.md` |
| M2 — Review loop hardening | approve/reject round-trip, card edit on decision, expired/unknown/double-tap edge cases, structured logging | 2026-07 | `docs/archive/M2_PLAN.md` |
| M3 — End-to-end dry run | slots drain in order, `recover_orphaned`, `requeue`, manual dry run (CUT-28) | 2026-07 | `docs/archive/M3_PLAN.md` |
| M4 — Ship v1 to Railway | Postgres, `Procfile`/`railway.toml`, webhook auto-register, **v1 live in prod** (CUT-34) | 2026-07-07 | `docs/archive/M4_PLAN.md` |
| M5 — Usability & operability | `brand.md` (was YAML), `/dev/status` + `/dev/run-cycle`, random image pick, reversible decisions, `/status` (CUT-35…41) | 2026-07 | `docs/issues/M5_ISSUES.md` |
| M6 — Operator console & feedback signal | `/generate` `/postnow` `/queue` `/requeue` `/pending`, photo→stock, reject-reason chips (`Feedback.reason`), `SCHEDULE_TZ`, slot catch-up (CUT-50…54) | 2026-07-09 | `docs/archive/M6_PLAN.md` |
| M7 — Instagram publisher | Graph adapter with container-resume + network-aware recovery, `/media` route, Meta approval, **first real post** (CUT-56…61) | 2026-10-05 | `docs/archive/M7_PLAN.md`, `docs/INSTAGRAM_RUNBOOK.md` |
| Owner review round | `/help` + command menu, `/stock` + 🚫 Ban image, no stock recycling, eval cost column | 2026-10-05 | `docs/issues/cutebot_issues_05_10_2026*.md` |
| M8 — Learning loop v1 | `app/learning.py` context in every caption, ✏️ Fix (edit = strongest signal, migration 0006), `--learning` eval flag, `/distill` brand proposals (CUT-63…67) | 2026-10-13 | `docs/archive/M8_PLAN.md` |

---

## Next (order decided 2026-07-08 — `docs/archive/POST_V1_REVIEW.md` has the reasoning)

Open owner step from M8: the paid learning A/B (`eval_models --learning` vs plain on
Opus 5.5 against a prod snapshot) — verdict goes in DEV_GUIDELINES "Model decision".

1. **M9 — Carousel posts** (graduated from backlog 2026-10-05):
   - Storage: JSON list column `image_refs` on `Post` (one migration; `image_ref`
     stays as the cover). A join table only when slides need per-slide captions.
   - Generation: 3–5 unused images in one vision call → connecting story + one caption.
   - Review: `sendMediaGroup` for the images; media groups can't carry inline buttons,
     so the Approve/Reject card is a second message (`send_suggestion` rework).
   - Publish: IG Graph N child containers → one `CAROUSEL` container → publish; the
     existing container-resume/idempotency path extends to it.
   - **First task: a `## Carousels` section in `brand.md` with 2–3 sample stories** —
     the prompt can't be written without knowing what a good one looks like.
2. **Later, relative order unchanged:** D — more review channels (re-aimed at
   **WhatsApp Business** for the Israeli market, not Discord/Slack) → F — analytics
   feedback → G — multi-tenant.

## Backlog (untriaged)

> Ideas land here (not in PRODUCT_SPEC); milestones graduate out of it.

- **Generated media from brand designs** (image + short clip from a designated design,
  e.g. a shirt print placed into a generated scene/story) — spec item B widened.
  Story text is trivial; image-to-image with a reference design is feasible but
  design-consistency is unreliable (~1 in 4 usable); video is expensive and keeps a
  specific print consistent across frames poorly. **Wait for cheaper, more
  design-consistent image/video models; re-check at each model-eval round.** When it
  looks viable: a half-day spike (10 generations from one design, judged in Telegram)
  before any milestone. Needs an explicit decision against PRODUCT_SPEC §3's
  stock-only rule and the brand's "real details, not stock photos" direction.

# M8 — Learning loop v1

> **Approved 2026-10-07.** Epic **CUT-63**; tickets **CUT-64** (M8.1), **CUT-65** (M8.2),
> **CUT-66** (M8.3), **CUT-67** (M8.4, stretch). This file is canonical for scope; the
> tickets carry the deliverable checklists.

## Context

M0–M7 shipped: the loop runs autonomously in prod, Instagram is live, and since M6 every
decision carries a signal — `Feedback(decision, reason)` with one-tap reject reasons
(voice / hebrew / image / boring, plus `ban_image`). The runtime model is now Claude Opus
5.5 (eval rounds 1–3). What's left is the thing the product is actually about: **each
batch is still memoryless** — `generate_batch` captions images from `brand.md` alone,
with no awareness of what the owner approved, rejected, or just posted
(`app/pipeline/generate.py:34`, `app/llm.py:_USER_TEMPLATE`).

M8 closes that gap with the cheapest possible mechanism: **prompt context derived from
the DB at generation time.** No fine-tuning, no embeddings, no new storage beyond the
edit columns the Fix button needs. The learned voice stays in text (brand file + the
owner's own approved posts), so it survives model switches.

**Locked decisions (2026-10-06):**
- **Few-shot = the owner's own approved posts**, not hand-written samples
  (POST_V1_REVIEW §5.0). `brand.md`'s "Sample posts" section stays as the cold-start
  fallback and is not touched by code.
- **One seam.** `llm.caption_image` gains an optional `context: LearningContext | None`.
  The pipeline builds it; the eval script can pass it or not — that's the A/B.
- **Edits are the strongest signal**, so the ✏️ Fix button (spec item C) ships *in* M8,
  not after it — otherwise the loop learns from approvals only.
- **Brand distillation** (`/distill` → proposed `brand.md` diff) is M8.4, a stretch:
  built only if M8.1–M8.3 land; otherwise it moves to M10. Carousel (M9) is not affected.
- **Graceful cold start.** With zero approved posts the context block is omitted
  entirely and generation behaves exactly as today. Nothing in M8 can make a batch worse
  than M7's.

## Approach

### M8.1 — Learning context: few-shot + reject hints + recent memory — `app/learning.py` (new), `app/llm.py` (CUT-64)

- `LearningContext` (pydantic): `examples: list[Example]` (`caption_he`, `caption_en`,
  `edited: bool`), `recent: list[str]`, `reject_hints: dict[str, int]`,
  `edits: list[Edit]` (`before`, `after`, `lang`).
- `build_context(session, settings) -> LearningContext`:
  - **examples** — the last `LEARNING_EXAMPLES` (default 5) posts with status
    `approved`/`publishing`/`published`, newest first; posts whose Feedback carries an
    edit rank first (they're the owner's *corrected* voice). Captions are the stored
    `caption_he`/`caption_en` (post-edit), never the disclaimer.
  - Pure selection — SQL on `posts` + `feedback`, no model call, no cache. ponytail: at
    `batch_size=5` a day this is one query per batch; add caching when it measurably
    matters.
- `llm.caption_image(brand, image, settings, context=None)`: when `context` has
  examples, render a `<approved_examples>` block into `_USER_TEMPLATE` after the brand
  guidelines — "These are recent posts the owner approved; match their voice, don't copy
  their content." Block omitted when empty.
- `generate_batch` builds the context once per batch and passes it to every call.
- Config: `LEARNING_ENABLED=true`, `LEARNING_EXAMPLES=5` (+ `env.example`).
- Tests: context from a seeded DB (edited-first ordering, zero-approved → empty block);
  prompt rendering asserts the block appears only with examples.

Same `build_context`, two more fields (one ticket — same module, same seam):

- **reject_hints** — count of reject reasons over the last `LEARNING_WINDOW_DAYS`
  (default 30), rendered as short directives only when non-zero:
  `hebrew` → "Recent rejections cited unnatural Hebrew — write as a native speaker would
  actually say it"; `voice` → "...off-brand voice — re-read the guidelines";
  `boring` → "...boring — take a sharper angle"; `image` → nothing (that's a selection
  problem, handled by `ban_image` in stock). One line per reason, weighted by count
  (≥3 → "repeatedly").
- **recent** — `caption_he` of the last `LEARNING_RECENT` (default 10) published posts,
  rendered as `<recent_posts>` with "don't repeat these themes, jokes, or openings" —
  the variety guard POST_V1_REVIEW §5.3 asks for (the signature line is a brand rule
  and stays; the *joke* must not repeat).
- Tests: hints only for reasons present in-window; recent capped at N; both omitted
  when empty.

### M8.2 — ✏️ Fix button (approve with edits) — `app/notifier/telegram.py`, migration `0006` (CUT-65)

- Review card gains a third button `✏️ Fix` (`fix:<post_id>`). Also offered on an
  already-approved card (edit while queued).
- Tap → bot sends `Reply to this message with the corrected Hebrew caption (post #42).
  Start with "en:" to fix the English instead.` using Telegram `ForceReply`. **Stateless:**
  the post id travels in the prompt text; `process_message` routes any owner message
  whose `reply_to_message` is from the bot and contains `#<id>` to the edit handler.
  No pending-edit table.
- Edit handler: replace `caption_he` (or `caption_en` with the `en:` prefix), then
  `handle_decision(approve)` if the post is `suggested`; if already `approved`, leave the
  queue alone. Either way write a `Feedback` row with the delta.
- **Schema (migration `0006`):** `Feedback.edit_lang: str | None` (`he`/`en`),
  `edit_before: Text | None`, `edit_after: Text | None`. Decision stays `approve`.
  ponytail: two text columns beat a JSON blob — the learning query reads them directly.
- Re-render the card: edit the message caption to the new full caption +
  `✅ Approved (edited)` so the owner sees the final text. Terminal statuses
  (publishing/published/failed) refuse the edit with the usual toast.
- `/help` and `setMyCommands` text updated; SCRIPTS_REFERENCE bot section updated.
- Tests: he/en routing, suggested→approved with Feedback delta, approved stays queued,
  published refused, non-owner ignored.

### M8.3 — Measure it + close out — `scripts/eval_models.py` (CUT-66)

- `--learning` flag: build the real `LearningContext` from the configured DB
  (`DATABASE_URL`, i.e. a prod snapshot or the local DB) and pass it to every candidate;
  default stays context-free. Header line records examples/recent/hints counts so the
  page says what the model saw.
- A/B = same model twice: `--models anthropic/claude-opus-5-5` run once without and once
  with `--learning`, rendered to two pages; the owner judges Hebrew side by side as in
  rounds 1–3 (~$0.60 per 10-image pair on Opus).
- Also add the learning counts to `/status` (`learning: 7 examples · 3 edits · 12
  recent`) so it's visible that the loop is feeding.
- Milestone close-out lives here too: ROADMAP ticked, SCRIPTS_REFERENCE, PRODUCT_SPEC §8
  A/C marked shipped, this plan archived to `docs/archive/M8_PLAN.md`.

### M8.4 — Brand distillation (stretch) — `/distill` (CUT-67)

Only if M8.1–M8.3 are done inside the milestone; otherwise → M10.

- `/distill` (owner command): an Opus call reads `brand.md` + the last 30 days of
  Feedback (reasons, edits, approved captions) and returns a **proposed unified diff** to
  `brand.md` with a one-paragraph rationale. DM'd with ✅ Apply / ❌ Discard.
- Apply = write the file on the Railway volume + `git`-free; the current file is backed
  up to `brand.md.bak` first. No automatic schedule — on demand only in v1.
- The same distillation prompt is the seed of the future onboarding flow
  (POST_V1_REVIEW §7, H2).

## Order and sizing

| # | Ticket | Item | Size | Depends on |
|---|---|---|---|---|
| M8.1 | CUT-64 | Learning context: few-shot + hints + memory | medium | — |
| M8.2 | CUT-65 | ✏️ Fix button + edit columns | medium | — (parallel with M8.1) |
| M8.3 | CUT-66 | `--learning` A/B + `/status` line + close-out | small | M8.1–M8.2 |
| M8.4 | CUT-67 | `/distill` (stretch) | medium | M8.3 |

M8.1 and M8.2 can run in parallel (different files). Rough total: 2–3 sessions.

## Out of scope

- Fine-tuning, embeddings/retrieval, per-image similarity — YAGNI at this volume.
- Weighting examples by engagement — needs analytics feedback (spec F).
- Editing the English independently of Hebrew *after* publish, diff view, partial
  edits — add when the edit log shows a need.
- Scheduled distillation, multi-brand contexts.

## DoD

- A batch generated in prod includes the owner's recent approvals as examples and
  avoids the last 10 published themes; `/status` shows the learning counts.
- ✏️ Fix round-trips on a test chat: reply → post approved with the corrected Hebrew,
  Feedback row carries before/after, card shows the final text.
- One A/B page (with vs without `--learning`, Opus 5.5, 10 images) reviewed by the owner;
  verdict recorded in DEV_GUIDELINES under "Model decision".
- `ruff check . && mypy app && pytest` green; docs (ROADMAP, SCRIPTS_REFERENCE, spec §8
  item A/C → shipped) updated; plan archived to `docs/archive/M8_PLAN.md`.

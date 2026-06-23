# CuteBot — Build Roadmap

> **Delivery plan** from the current skeleton to a shipped v1 and beyond. This is the
> *sequencing* doc; `PRODUCT_SPEC.md` remains the source of truth for *what* CuteBot is.
> When scope changes, change the spec first, then resequence here.

## Locked decisions

- **Captioning = vision.** Generation passes the actual stock image to Claude
  (multimodal); the caption is grounded in what the model genuinely sees, not in a
  filename or tags. (PRODUCT_SPEC §3.)
- **v1 "done" = prove the loop.** v1 ships the full generate→review→queue→publish cycle
  deployed and autonomous, with **stub** publishers. The first *real* network is the
  first post-v1 item (M-E). Telegram review is real from M0.
- **Bilingual (Hebrew-first) + mandatory disclaimer.** Every post is generated in Hebrew
  (primary) and English, stored as `caption_he`/`caption_en`; Hebrew quality is a hard
  gate. A cute, configurable CuteBot disclaimer is auto-appended **in code** to every
  post (review + publish). Language set is config-driven (default `he`+`en`); changing
  the primary is deferred. (PRODUCT_SPEC §1, §3, §7.)

## Current state (skeleton — green)

The four-stage loop is fully wired and passes ruff/mypy/pytest/CI: config, async DB,
models (`Post`/`Batch`/`Feedback`), the `app/llm.py` seam (real LiteLLM call + offline
stub), all four pipeline stages, APScheduler (generation cron + posting slots), the
Telegram notifier (text DM, inline Approve/Reject, webhook **and** polling), and
logging-stub publishers for IG/TikTok/X.

**The gap:** the spec is now *image-first off a stock library*; the code still does
text-only generation. `Post` has no `image_ref`, there's no `STOCK_IMAGES_DIR`,
`generate.py` never touches an image, and Telegram sends text not a photo. Closing this
is M0.

---

## M0 — Image-first generation (close the spec↔code gap)

> Detailed, approved implementation plan: **`M0_PLAN.md`**.

The headline of v1. Reorder generation to *image → caption* with vision.

- [ ] `Post`: add `image_ref`, `caption_he`, `caption_en` (replacing the single
      `caption`); introduce **Alembic** and write the first migration.
- [ ] Config + `env.example`: `STOCK_IMAGES_DIR`, `PRIMARY_LANGUAGE` (default `he`),
      `SECONDARY_LANGUAGES` (default `en`), `POST_DISCLAIMER` (cute bilingual default).
- [ ] `app/stock.py` — enumerate the stock library, pick an unused image per post.
- [ ] `app/disclaimer.py` — compose the disclosure line from the template; one helper
      used by both review and publish so the guarantee lives in one place.
- [ ] Rework `app/llm.py`: take an image, return `caption_he`/`caption_en`/
      `visual_concept`/`rationale` via a **multimodal** Claude call; prompt demands
      native-quality Hebrew (primary) + English; offline stub returns both languages.
- [ ] `generate.py`: select image → caption it (bilingual) → persist with `image_ref`.
- [ ] `app/schemas.py`: `PostSuggestion` carries `caption_he`/`caption_en`; input carries
      the chosen image.
- [ ] Telegram `sendPhoto` (image + Hebrew & English caption + disclaimer +
      Approve/Reject) so the reviewer sees the real, final post.
- [ ] Publishers compose the final caption (he + en + disclaimer) and carry `image_ref`
      through `publish()` (stubs log it).
- [ ] Tests: image-first path; **assert the disclaimer is present** on every rendered and
      published caption; assert both language fields are populated. Keep the suite offline.

**DoD:** `/dev/generate` produces posts each bound to a real stock image with a
vision-grounded **Hebrew + English** caption; the Telegram DM shows that photo with both
languages and the disclaimer. ruff/mypy/pytest green.

## M1 — Real generation, validated

- [ ] Run against a live `ANTHROPIC_API_KEY`; verify multimodal output + structure.
- [ ] **Hebrew quality gate** — native review of a real batch; tune the prompt until the
      Hebrew reads naturally (not translated-sounding) and the English matches in voice.
- [ ] Realistic `brand.yaml`; prompt tuning for voice + hard-rule adherence.
- [ ] `llm.py`: retries, timeout, `max_tokens`/cost guardrails, graceful failure.
- [ ] Image rotation/dedup so the pool cycles before reuse.

**DoD:** a real batch reads cleanly on-brand in **native-quality Hebrew + English** with
the disclaimer present, against a sample stock library; failures degrade gracefully.

## M2 — Review loop hardening

- [ ] Real approve/reject round-trip against a test chat (photo message → callback → state).
- [ ] Edit the message on decision (strike-through / "✅ Approved" / "❌ Rejected").
- [ ] Edge cases: expired buttons, unknown/double-tapped post (idempotency already holds).
- [ ] Structured logging across the webhook + decision path.

**DoD:** a human can clear a full batch from Telegram; every decision is a `Feedback` row
and the message reflects the outcome.

## M3 — End-to-end dry run

- [ ] Confirm posting slots fire and drain front-of-queue in order.
- [ ] Exercise crash/retry idempotency (post claimed `publishing` before any send).
- [ ] Publishers still stubs, now carrying the image.

**DoD:** an approved post flows from queue to (stub) publish at a real slot, once, with
correct state transitions.

## M4 — Ship v1 to Railway

- [ ] Postgres on Railway; run Alembic migrations on deploy.
- [ ] Webhook mode (public HTTPS) + secret; env wiring; healthcheck.
- [ ] Smoke test the deployed loop end-to-end.

**DoD:** the loop runs autonomously in production with stub publishers — **v1 shipped.**

---

## Post-v1 (PRODUCT_SPEC §8, resequenced by value)

### dev note - move E to be num 4 in order (just before more review channels)

1. **E — first real publisher** (one network: Instagram Graph or X) with OAuth + media
   upload. Highest external value; turns stub-publish into real reach.
2. **A — learning loop v2** — few-shot from accumulated approvals; learn from rejections.
3. **C — inline editing** — "Approve with edits" as a stronger training signal.
4. **D — more review channels** — Discord/Slack notifier adapters.
5. **F — analytics feedback** — pull post performance back as a generation signal.
6. **G — multi-brand / multi-tenant** — scope everything by `brand_id`.
7. **B — image generation** — *deferred for the foreseeable future* (v1 is stock-image).

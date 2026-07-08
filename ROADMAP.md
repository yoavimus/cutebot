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

> Shipped ✅ — plan archived at **`docs/archive/M0_PLAN.md`**.

The headline of v1. Reorder generation to *image → caption* with vision.

- [x] `Post`: add `image_ref`, `caption_he`, `caption_en` (replacing the single
      `caption`); introduce **Alembic** and write the first migration.
- [x] Config + `env.example`: `STOCK_IMAGES_DIR`, `PRIMARY_LANGUAGE` (default `he`),
      `SECONDARY_LANGUAGES` (default `en`), `POST_DISCLAIMER` (cute bilingual default).
- [x] `app/stock.py` — enumerate the stock library, pick an unused image per post.
- [x] `app/disclaimer.py` — compose the disclosure line from the template; one helper
      used by both review and publish so the guarantee lives in one place.
- [x] Rework `app/llm.py`: take an image, return `caption_he`/`caption_en`/
      `visual_concept`/`rationale` via a **multimodal** Claude call; prompt demands
      native-quality Hebrew (primary) + English; offline stub returns both languages.
- [x] `generate.py`: select image → caption it (bilingual) → persist with `image_ref`.
- [x] `app/schemas.py`: `PostSuggestion` carries `caption_he`/`caption_en`; input carries
      the chosen image.
- [x] Telegram `sendPhoto` (image + Hebrew & English caption + disclaimer +
      Approve/Reject) so the reviewer sees the real, final post.
- [x] Publishers compose the final caption (he + en + disclaimer) and carry `image_ref`
      through `publish()` (stubs log it).
- [x] Tests: image-first path; **assert the disclaimer is present** on every rendered and
      published caption; assert both language fields are populated. Keep the suite offline.

**DoD:** `/dev/generate` produces posts each bound to a real stock image with a
vision-grounded **Hebrew + English** caption; the Telegram DM shows that photo with both
languages and the disclaimer. ruff/mypy/pytest green.

## M1 — Real generation, validated ✅

> Shipped ✅ — plan archived at **`docs/archive/M1_PLAN.md`**.

- [x] Run against a live API key; verify multimodal output + structure.
- [x] **Hebrew quality gate** — native review of a real batch; Hebrew reads naturally,
      not translated; English matches in voice. GPT-4o cleared the gate in comparison
      testing; **runtime model still open** (config default stays Claude Sonnet) — see
      DEV_GUIDELINES "Model decision".
- [x] Realistic `brand.yaml`; prompt tuning for voice + hard-rule adherence.
- [x] `llm.py`: retries, timeout, `max_tokens`/cost guardrails, graceful failure.
- [x] Image rotation/dedup so the pool cycles before reuse.

**DoD:** a real batch reads cleanly on-brand in **native-quality Hebrew + English** with
the disclaimer present, against a sample stock library; failures degrade gracefully.

## M2 — Review loop hardening

> Shipped ✅ — plan archived at **`docs/archive/M2_PLAN.md`**.

- [x] Real approve/reject round-trip against a test chat (photo message → callback → state).
- [x] Edit the message on decision ("✅ Approved" / "❌ Rejected"), buttons removed.
- [x] Edge cases: expired buttons (graceful), unknown post ("Post not found"), double-tap ("Already …").
- [x] Structured logging across the webhook + decision path.

**DoD:** a human can clear a full batch from Telegram; every decision is a `Feedback` row
and the message reflects the outcome.

## M3 — End-to-end dry run

> Shipped ✅ — plan archived at **`docs/archive/M3_PLAN.md`**.

- [x] Confirm posting slots fire and drain front-of-queue in order (test-verified).
- [x] Crash recovery: `recover_orphaned` sweeps `PUBLISHING` on startup; `requeue` + `/dev/requeue/{id}` for `FAILED` posts.
- [x] Publishers still stubs, carrying the image.
- [x] **Manual dry run** (CUT-28): real slot, real SQLite, approve via Telegram → publish in order; kill mid-publish → restart recovers; fail → requeue → publish.

**DoD:** an approved post flows from queue to (stub) publish at a real slot, once, with
correct state transitions; a crash mid-publish self-heals, never double-posts.

## M4 — Ship v1 to Railway

> Shipped ✅ & **verified in prod** — plan archived at **`docs/archive/M4_PLAN.md`**.

- [x] Postgres URL normalization (`app/db.py`: coerces `postgresql://`/`postgres://` → `+asyncpg`).
- [x] `Procfile` + `railway.toml`: `alembic upgrade head && uvicorn … --host 0.0.0.0 --port $PORT`.
- [x] Webhook auto-registered on prod startup; env inventory documented in `env.example`.
- [x] Railway provisioned: Postgres plugin + persistent volume (`/data`), all env vars set.
- [x] `/health` green; dev routes 404 in prod; generation fired; Telegram approval via real webhook confirmed.
- [x] **Posting slot → stub-publish confirmed in prod** (CUT-34): post 1 went
      `suggested → approved → published` (published_at `2026-07-07 18:49:00`), verified via
      `railway connect Postgres` (scheduler is the only publish trigger in prod).

**DoD:** the loop runs autonomously in production with stub publishers — **v1 shipped ✅.**

## M5 — Post-v1 usability & operability

> Shipped ✅ — tickets CUT-35 through CUT-41.

- [x] **M5.1** — brand file switched from YAML to Markdown (`brand.example.md`; config default + `env.example` updated; code was already `read_text` verbatim).
- [x] **M5.2** — `GET /dev/status` (20 most-recent posts, optional `?status=` filter); `POST /dev/run-cycle` (generate→auto-approve→publish in one call); `/dev/generate` and `/dev/publish-next` return full post objects instead of just IDs.
- [x] **M5.3** — `select_images` uses `random.sample`; only images tied to APPROVED/PUBLISHING/PUBLISHED posts are blocked — rejected/suggested images are free again.
- [x] **M5.4** — `handle_decision` allows APPROVED↔REJECTED flips (each writes a Feedback row); PUBLISHING/PUBLISHED hard-blocked. Telegram `mark_decided` leaves the opposite button until a post publishes.
- [x] **M5.5** — `GENERATION_CRON` and `POSTING_SLOTS` documented in `env.example` and `SCRIPTS_REFERENCE.md` (edit + redeploy; times are UTC).
- [x] **M5.6** — Telegram `/status` command: DM the bot `/status` → counts by status, queue depth, last 5 published posts. Owner-gated; works via webhook and long-poll. Makes prod state visible without DB access.

**DoD:** pipeline is pleasant to operate — status visible in one curl (dev) or Telegram `/status` (prod), decisions reversible, images random, brand file editable by non-technical owners.

---

## Post-v1 (decided 2026-07-08 — see `docs/POST_V1_REVIEW.md` for the reasoning)

Standing task (not a milestone): **model eval** — `scripts/eval_models.py` bake-off,
native Hebrew review by the owner. Round 1: Claude Sonnet / Claude Opus / same-tier
GPT (pending Anthropic credits); round 2 adds Gemini. Resolves the DEV_GUIDELINES
"Model decision" open item.

1. **M6 — operator console & feedback signal** (small–medium): full Telegram command
   set (`/generate [N]`, `/postnow [id]`, `/queue`, `/requeue <id>`, `/pending`,
   photo-upload-to-stock), one-tap **reject reasons** (voice / Hebrew / image / boring
   + skip; new `Feedback.reason` column), timezone-aware posting slots
   (`SCHEDULE_TZ`, default `Asia/Jerusalem`), posting-slot misfire catch-up, and
   review-DM send-failure logging. Plan: **`M6_PLAN.md`**.
2. **M7 — first real publisher: Instagram** (spec E; medium–large): Instagram Graph
   API with OAuth + media upload; carousel/multi-image design decided here (schema:
   post-images table). **Meta business-verification / app-review paperwork starts
   during M6** — weeks of external lead time.
3. **M8 — learning loop v1** (spec A; medium): few-shot from accumulated approvals +
   reject-reason conditioning; recent-post memory ("don't repeat these"); measured
   with the eval harness. **Brand distillation** (strong model proposes `brand.md`
   diffs from feedback, owner approves in Telegram) lands here or M9.
4. **Later, relative order unchanged:** C — approve-with-edits → D — more review
   channels (re-aimed at **WhatsApp Business** for the Israeli market, not
   Discord/Slack) → F — analytics feedback → G — multi-tenant → B — image generation
   (*deferred for the foreseeable future*).

## Backlog (untriaged)

> Ideas land here (not in PRODUCT_SPEC); milestones graduate out of it.

- *(empty — the 2026-07-08 review triaged everything into the sequence above)*

# CuteBot — Product Spec (source of truth)

> This document is the **single source of truth** for CuteBot's scope, architecture,
> and roadmap. Code and other docs defer to it. Keep it current; when scope changes,
> change it here first.

## 1. Problem & product

Small brands need a steady, on-brand social presence but can't staff a copywriter or
a daily posting habit. CuteBot is an **autonomous background pipeline** that drafts
on-brand posts, gets a one-tap human approval, queues the approved ones, and
publishes them on a fixed schedule — learning from every approve/reject.

**Co-equal principles:**
- **Human-in-the-loop, always.** Nothing publishes without explicit approval.
- **The human owns schedule & quality.** CuteBot proposes; the human disposes.
- **It learns.** Every decision is feedback that improves future generations.

## 2. The four-stage cycle

| Stage | Trigger | Module | Output |
|-------|---------|--------|--------|
| 1. Generate | `GENERATION_CRON` (default 9am daily) | `app/pipeline/generate.py` | N `Post` rows, status `suggested` |
| 2. Review | immediately after generation | `app/pipeline/review.py` + `app/notifier/` | each post DM'd with Approve/Reject; `Feedback` row on decision |
| 3. Queue | on Approve | `app/pipeline/queue.py` | post moves to status `approved`, ordered in queue |
| 4. Publish | each slot in `POSTING_SLOTS` | `app/pipeline/publish.py` + `app/publishers/` | front-of-queue post → all networks; status `published` |

### Post lifecycle (status state machine)

```
suggested ──approve──▶ approved ──slot reached──▶ publishing ──ok──▶ published
    │                                                  │
    └──reject──▶ rejected                              └──error──▶ failed
```

Rejections and approvals both write a `Feedback` row (the training signal).

## 3. Architecture

A **single in-process FastAPI app** hosts both the HTTP surface (health, Telegram
webhook) and the **APScheduler** jobs (generation + posting slots). No Redis, no
external worker in v1 — the scheduler runs in the app process.

```
                ┌──────────────────────── FastAPI app ───────────────────────┐
                │                                                             │
  cron 9am ────▶│  generate.py ──▶ llm.py (LiteLLM→Claude) ──▶ Post(suggested)│
                │       │                                                     │
                │       ▼                                                     │
                │  review.py ──▶ notifier/telegram.py ──DM──▶  📱 you         │
                │                         ▲  Approve/Reject callback          │
   webhook ─────▶  /telegram/webhook ─────┘                                   │
                │       │                                                     │
                │       ▼                                                     │
                │  queue.py (Post: approved, ordered)                         │
                │       │                                                     │
  cron slots ──▶│  publish.py ──▶ publishers/{instagram,tiktok,x}.py ──▶ 🌐  │
                └─────────────────────────────────────────────────────────────┘
                                   │
                              PostgreSQL (async SQLAlchemy)
```

### Key design decisions
- **Single LLM agent, provider-agnostic.** All model calls go through `app/llm.py`,
  which wraps LiteLLM. Model is set by `DEFAULT_LLM_MODEL` (default
  `anthropic/claude-sonnet-4-6`). Switching providers is a one-env-var change.
- **Notifier is an interface.** `app/notifier/base.py` defines `Notifier`; Telegram is
  the first adapter. Discord/Slack are drop-in future adapters.
- **Publishers are interfaces.** `app/publishers/base.py` defines `Publisher`; each
  network is an adapter. v1 ships functional **stubs** that log instead of posting.
- **Structured data over parsing.** The LLM returns structured post objects (caption +
  visual concept + rationale), validated by a Pydantic schema — never free-text parsing.

## 4. Data model (`app/models.py`)

- **Post** — `id, caption, visual_concept, rationale, status, created_at, decided_at,
  published_at, queue_position, source_batch_id`.
- **Feedback** — `id, post_id, decision (approve|reject), created_at` — the training
  signal. Future: free-text reason, edit deltas.
- **Batch** — `id, created_at, brand_snapshot, model, size` — provenance for a
  generation run.

## 5. Stack

- Python 3.12, FastAPI, async SQLAlchemy 2.x, Pydantic v2 / pydantic-settings
- APScheduler (in-process)
- LiteLLM (Claude default)
- httpx (Telegram Bot API + publisher HTTP)
- PostgreSQL (SQLite for local/test)
- ruff + mypy + pytest

## 6. Configuration

All via env (`app/config.py`, `pydantic-settings`). See `env.example`. Brand
guidelines live in a `brand.yaml` file (see `brand.example.yaml`) and are injected
into the generation prompt verbatim.

## 7. Security & safety

- **No secrets in code or logs.** All credentials via env.
- **Approval gate is load-bearing.** The publish path must only ever act on posts in
  `approved` status; never auto-approve.
- **Webhook authenticity.** The Telegram webhook validates the secret token before
  acting on a callback.
- **Idempotent publishing.** A post moves to `publishing` before network calls so a
  retry/crash can't double-post; success → `published`, failure → `failed`.

## 8. Roadmap (post-v1)

Deferred — not in v1, in rough priority order:

- **A. Learning loop v2** — fine-tune the prompt from accumulated `Feedback`
  (few-shot selection of past approvals; learn from rejections).
- **B. Image generation** — turn `visual_concept` into an actual image (e.g. an
  image model) and attach it to the post and to publishing.
- **C. Inline editing** — "Approve with edits" in Telegram; capture the edit as a
  stronger training signal than approve/reject.
- **D. More review channels** — Discord and Slack notifier adapters.
- **E. Real publishers** — implement Instagram Graph, TikTok, and X adapters with
  OAuth + media upload (replacing the v1 stubs).
- **F. Analytics feedback** — pull post performance back in as a generation signal.
- **G. Multi-brand / multi-tenant** — scope everything by `brand_id`.

## 9. Out of scope (v1)

Multi-tenant, web dashboard, image generation, real network publishing, analytics,
payment, and per-post scheduling beyond fixed daily slots.

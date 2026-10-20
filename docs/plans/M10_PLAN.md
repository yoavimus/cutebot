# M10 — Tenant seam (plan, approved 2026-10-21)

> Phone review copy: https://claude.ai/code/artifact/346305df-1d1c-4b2a-9760-805f5ebf7a19
> (comments there are folded back here; this file is canonical).
> Tickets: **CUT-73** epic · **CUT-74** M10.1 · **CUT-75** M10.2 · **CUT-76** M10.3.
> Kickoff: `docs/plans/M10_KICKOFF.md`. Decisions under "Risks" confirmed 2026-10-21.

## Goal

M10 puts a `brand_id` under every row and moves the brand text into the database, so M11–M16
are built per-brand from day one. The owner sees no change: one default brand row is created
from today's env and `brand.md`; console, cards and schedule behave exactly as now. Two
one-column extras ride along: `cost_usd` per post/batch, and the previous brand text on the
row (replacing `brand.md.bak`). Size: 2–3 days, two sessions.

## Scope

**In.** `brands` table with one default row. `brand_id` on posts, batches, feedback,
banned_images, settings, backfilled. Brand text on the row; `brand.md` is the seed file;
`/distill` edits the row and keeps the previous text. Telegram chat id resolves to a brand.
Every pipeline function takes the brand it works for. Tokens + `cost_usd` per post, summed
per batch, one cost line in `/status`.

**Out.** Onboarding, per-brand Instagram tokens, per-brand schedule jobs, usage caps,
billing, media storage. One bot, one owner chat, one schedule per process until M13.

## Data model

Two Postgres-only migrations (SQLite tests use `create_all`). New columns: nullable →
backfill → NOT NULL, by hand.

| Table | Change | Notes |
|---|---|---|
| `brands` (new, 0009) | `id, name, telegram_chat_id (unique), brand_text, brand_text_prev, stock_prefix (default ""), created_at, updated_at` | Migration inserts row 1 from `TELEGRAM_CHAT_ID`, name `default`, empty text. Boot seeds `brand_text` from `BRAND_FILE` when empty (the volume file is read by the app, not Alembic) |
| `posts`, `batches`, `feedback`, `settings` (0009) | `brand_id` FK → brands, NOT NULL, indexed, backfilled to 1 | `settings` PK becomes (`brand_id`, `key`) |
| `banned_images` (0009) | `brand_id`; PK becomes (`brand_id`, `image_ref`) | tiny table, recreate |
| `posts` (0010) | `tokens_in`, `tokens_out` (int, null), `cost_usd` (numeric 10,6, null) | from the LiteLLM usage block already read in `app/llm.py` |
| `batches` (0010) | `cost_usd` (numeric 10,6, null) | sum of its posts |

Mode stays in `settings` (per brand). `Batch.brand_snapshot` stays. `brand.md.bak` → `brand_text_prev`.

## Code changes

Rule: every function that reads/writes posts, feedback, stock, settings or brand text takes a
`Brand` (or id). No module reads `settings.telegram_chat_id` or `brand.md` to find out who it
works for.

| File | Change |
|---|---|
| `app/models.py` | `Brand`; `brand_id` on `Post`, `Batch`, `Feedback`, `BannedImage`, `Setting`; `tokens_in/out`, `cost_usd` on `Post`; `cost_usd` on `Batch` |
| `app/brands.py` (new) | `default(session)`, `by_chat_id(session, chat_id)`, `ensure_default(session, settings)` at boot (creates row 1, seeds text from `BRAND_FILE` when empty), `text(brand)` with today's built-in fallback |
| `app/brand.py` | `load_brand` stays as the seed reader only |
| `app/distill.py` | `propose(session, settings, brand)` reads the row; `apply` writes the row, old text → `brand_text_prev`; the changed-since guard compares the row |
| `app/pipeline/generate.py` | `generate_batch(session, brand, n)`; sets `brand_id`, tokens, `cost_usd`; `batch.cost_usd` |
| `app/llm.py` | `cost_usd(model, tokens_in, tokens_out)` moved in from `scripts/eval_models.py` |
| `app/learning.py`, `app/stock.py`, `app/prefs.py`, `app/pipeline/queue.py`, `review.py`, `publish.py` | every query filtered by `brand_id`; stock lists `stock/<stock_prefix>/` (empty = today's folder); `prefs.*` take `brand_id` |
| `app/scheduler.py` | slot + generation jobs loop over brands; mode pause stays process-wide |
| `app/notifier/telegram.py` | `process_message`/`process_callback` resolve chat → brand via `brands.by_chat_id`; unknown chat ignored. `send_suggestion`/`send_message` address `brand.telegram_chat_id` (env chat id = boot-warning fallback). Handlers get `brand`. `/status` + cost line (30 d). `/brand` (typed only) shows the text |
| `app/main.py` | `ensure_default` in lifespan before the scheduler loads |
| `scripts/brand_sync.py` (new) | `push` (brand.md → row), `pull` (row → brand.md) |
| `tests/` | conftest creates the default brand; new tests: chat resolution, per-brand filtering, distill on the row, cost columns |
| Docs | PRODUCT_SPEC §data, ROADMAP M10 row, DEV_GUIDELINES (brand text in DB; brand.md = seed), SCRIPTS_REFERENCE, CLAUDE.md |

## Tickets

| Ticket | What | Done when | Size |
|---|---|---|---|
| CUT-73 M10 epic | close-out checklist | all three Done, owner confirms `/console` unchanged | — |
| CUT-74 M10.1 Brand row + `brand_id` seam | `Brand`, migration 0009 + backfill, `app/brands.py`, `ensure_default`, chat → brand, brand threaded through generate/learning/stock/prefs/queue/review/publish/scheduler/telegram, tests | gate green; fresh SQLite boot creates brand 1 + seeds text; dry-run cycle writes `brand_id=1` on batch/posts/feedback; unknown chat ignored | 1.5–2 d |
| CUT-75 M10.2 Brand text in the DB | `/distill` on the row (+ `brand_text_prev`); `/brand`; `scripts/brand_sync.py push|pull`; `brand.md` documented as seed | gate green; `test_distill` on the row; `pull` reproduces the row byte-for-byte; docs | 0.5 d |
| CUT-76 M10.3 Cost per post/batch | migration 0010; `llm.cost_usd`; stored in `generate_batch`; eval imports helper; `/status` cost line | gate green; stub batch → null cost, usage → computed; eval output unchanged | 0.5 d |

## Order and sessions

Branch `m10-tenant`, one checkout, no worktrees. Sessions split by model, not ticket.

1. **Session 1 — Opus 5.5: M10.1.** Commits on the branch, stops.
2. **Session 2 — Sonnet 5.5: M10.2, M10.3, close-out.** Pushes, opens the PR, never merges.
3. **Planner: review, merge, deploy, tickets.** After deploy: `railway connect postgres` → no NULL `brand_id`.

Kickoff: `docs/plans/M10_KICKOFF.md` on approval (common block + one block per session).

## Risks and decisions to confirm

- **Migration on live data** (NOT NULL columns, two PK changes). Mitigation: nullable → backfill → NOT NULL; recreate `banned_images`; dry run on a `pg_dump` copy first. Rollback: `downgrade` + previous Railway deploy.
- **Brand text source of truth moves.** After M10, editing `brand.md` on the volume does nothing until `brand_sync.py push`. DB wins, file = seed. **Confirmed.**
- **Stock per brand by sub-folder** `stock/<prefix>/`, empty for default. **Confirmed.** (M11 moves media anyway.)
- **Schedule stays process-wide** until M13; jobs loop over brands. **Confirmed.**
- **Feedback gets its own `brand_id`** (reachable via post, but keeps learning queries to one filter). **Confirmed.**
- **Cost needs prices:** unknown model id in `litellm.model_cost` → null, never guessed.

## Done when

- [ ] Gate green: `ruff check . && mypy app && pytest`
- [ ] Fresh SQLite boot creates brand 1, seeds text from `brand.md`, `/health` 200
- [ ] 0009 + 0010 dry run on a copy of the prod DB
- [ ] Railway deploy SUCCESS; log shows `0008 -> 0009`, `0009 -> 0010`
- [ ] `railway connect postgres`: no NULL `brand_id` anywhere
- [ ] Phone: `/console` home identical; Generate 1 → card → Keep; `/status` cost line; `/brand`; `/distill` round-trip edits the row
- [ ] `brand_sync.py pull` reproduces the row; `push` after an edit updates it
- [ ] Docs updated; plan + kickoff archived
- [ ] Linear: tickets Done with ship comments, epic closed

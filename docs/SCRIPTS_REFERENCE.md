# CuteBot — Scripts Reference

> **Shell**: zsh on WSL2 (Linux). `&&` chaining works; `curl` is real curl.
> **Project root**: `/home/yoav/Projects/cutebot`
> Keep this file in sync — every reusable command/script lives here.

## Environment

Per the global standard: **`uv` with a pinned interpreter (Python 3.12)**. The
machine's default `python3` is 3.14, which fails to build some 3.12-only wheels — `uv`
provisions 3.12 for you.

```bash
# Install uv once (if missing): curl -LsSf https://astral.sh/uv/install.sh | sh
uv python pin 3.12                  # writes .python-version (already committed)
uv venv                             # create .venv on Python 3.12 (auto-installs it)
source .venv/bin/activate           # prompt shows (.venv)
uv pip install -r requirements.txt  # install deps
cp env.example .env                 # then fill in secrets
```

## Run the app (API + in-process scheduler)

```bash
# Local: bind 127.0.0.1, port 8002 (8000/8001 taken by other projects)
uvicorn app.main:app --reload --host 127.0.0.1 --port 8002
```

Health check:

```bash
curl -s http://127.0.0.1:8002/health        # -> {"status":"ok"}
```

## Trigger pipeline stages manually (no waiting for cron)

Dev-only endpoints (enabled when `APP_ENV=development`; all return 404 in prod):

```bash
# See pipeline state at a glance — replaces raw sqlite3 queries
curl -s http://127.0.0.1:8002/dev/status | jq
curl -s 'http://127.0.0.1:8002/dev/status?status=approved' | jq

# Generate a batch now; returns post objects with truncated captions
curl -s -X POST http://127.0.0.1:8002/dev/generate | jq

# Drain one post from the queue; returns the published post's state
curl -s -X POST http://127.0.0.1:8002/dev/publish-next | jq

# Full cycle in one call: generate → auto-approve → publish (dev only, never prod)
curl -s -X POST http://127.0.0.1:8002/dev/run-cycle | jq

# Requeue a specific FAILED post
curl -s -X POST http://127.0.0.1:8002/dev/requeue/42 | jq
```

## Public media route (M7 — Instagram Graph fetches images here)

```bash
# Not dev-gated — Graph is an anonymous fetcher. Always returns JPEG (Pillow
# conversion), 404s outside stock_images_dir.
curl -s -o out.jpg -w '%{http_code} %{content_type}\n' http://127.0.0.1:8002/media/<image_ref>
```

## Telegram review channel

```bash
# Long-polling (no public URL needed) — runs the bot loop locally:
python -m app.notifier.telegram poll

# Webhook mode (needs TELEGRAM_WEBHOOK_BASE set to a public HTTPS URL):
python -m app.notifier.telegram set-webhook
python -m app.notifier.telegram delete-webhook
```

### Bot commands (owner-gated; work in dev and prod — M6, M9)

One table (`_COMMANDS` in `app/notifier/telegram.py`) drives both the native "/" menu and
the grouped `/help`. Output is HTML; values are escaped.

```
Pipeline
/console                pin a status card with a button grid; every tap runs a command and
                        rewrites the card in place (Status · Queue · Pending / Generate · Stock ·
                        Schedule / Mode · Help — publish from a /queue ▶ row, not the grid)
/status                 mode header, counts by status, queue depth, stock, learning verdicts, last 5 published
/queue                  approved posts in order — each row has ▶ Post now / ❌ Drop buttons
/pending                re-DM undecided suggestions (count line first)
/generate [n]           generate n suggestions now (default BATCH_SIZE) + DM for review
/postnow [id]           publish front-of-queue, or a specific APPROVED post (refused in learning mode / on kept)
/requeue <id>           move a FAILED post back into the queue (typed only — not in the menu)
Library
/stock [unban <file>]   unused / used / banned counts; banned rows have ♻️ Unban buttons
Settings
/schedule               current generation cron + slots (SCHEDULE_TZ) with next-run times
/schedule post 12:00,18:00      1–6 HH:MM slots, applied without a restart
/schedule generate 09:00        daily at, or a raw cron: "0 9 * * 1-5"
/schedule reset                 back to the env defaults
/mode [learning|live]   bare /mode shows a confirm keyboard; learning: ✅ = 📚 Keep, slots paused, /postnow refused
/distill                propose a brand.md revision from recent feedback (✅ Apply / ❌ Discard)
/help                   grouped list (also /start)
(photo DM)              save the photo into the stock library
```

**Slash menu** — registered with `setMyCommands` at startup, scoped to the owner chat
(`scope: {type: chat, chat_id}`); if Telegram rejects the scoped call it falls back to the
default scope so the menu never disappears. Labels are one or two plain words (≤ 16 chars); `/requeue`
is not in the menu. After changing labels, restart so the app re-registers them.

**Console card** — `_CONSOLE_GRID` + `_CardWriter` in `app/notifier/telegram.py`: a tap on
the pinned card runs the same handler as the typed command with a notifier whose
`send_message` edits the card (`editMessageText`) and keeps the grid under the output. Taps
on an ordinary DM (a `/queue` list) reply with a new message as before. The card is one
message, so Telegram's 4096-char limit applies; `/stock` and `/queue` already cap their rows.

**Card keyboard** (M9.1): `✅ Approve · 📚 Keep · ✏️ Fix` / `🤏 Almost · ❌ Reject`.

| Callback data | Does |
|---|---|
| `approve:<id>` | approve (publish) — in learning mode behaves as Keep; also 📤 Publish after all on a kept card |
| `keep:<id>` | 📚 approved for learning only — never queued, never published |
| `reject:<id>` → `reason:<id>:<chip\|skip\|ban_image>` | reject, optional reason chip; `ban_image` retires the photo |
| `almost:<id>` → `almostr:<id>:<chip\|skip>` | 🤏 right direction, flawed; post ends `rejected`, Feedback `almost` |
| `retry:<id>` | 🔁 re-caption an Almost'd post's image as a new card (`Post.retry_of`) |
| `fix:<id>` | ForceReply prompt for the corrected caption |
| `postnow:<id>` / `drop:<id>` | `/queue` row buttons → `publish_by_id` / reject (owner-gated) |
| `unban:<ref>` | `/stock` row button; refs over Telegram's 64-byte callback cap show the typed command instead |
| `mode:learning` / `mode:live` / `mode:cancel` | `/mode` confirm keyboard (owner-gated) |
| `distill:apply\|discard:<token>` | `/distill` proposal (owner-gated) |

Rejecting a post (❌) offers one-tap reason chips (voice/hebrew/image/boring/skip) —
stored on `Feedback.reason`, the learning-loop signal — plus **🚫 Ban image**, which also
retires the photo from future batches (`banned_images` table; the file stays on disk so
`/media` keeps serving). Images used by approved/kept/published posts are never recycled;
a short batch DMs a low-stock warning.

**✏️ Fix** (M8.2) sits next to Approve/Reject (and on approved cards). Tap → the bot
asks you to *reply* with the corrected Hebrew caption (prefix `en:` to fix the English
instead). The reply approves a suggested post (an approved one keeps its queue spot),
re-renders the card as `✅ Approved (edited)`, and writes a `Feedback` row with
`edit_lang`/`edit_before`/`edit_after` — the strongest learning signal, fed back into
generation by `app/learning.py`. Published/rejected posts refuse edits. No pending-edit
state: the post id travels in the prompt text.

**/distill** (M8.4, on demand only): one LLM call reads `brand.md` + the learning context
(approved posts, edits, reject reasons) and DMs a unified diff + rationale. Nothing is
written until ✅ Apply (owner-only), which first copies the file to `brand.md.bak`.
Proposals live in memory — after a restart the buttons answer "expired, run /distill
again"; Apply also refuses if `brand.md` changed since the proposal.

## Model eval (Hebrew quality bake-off)

```bash
# Same images + brand file across candidate models, side-by-side Markdown for review.
# Keys come from .env; a candidate without its provider key yields the offline stub.
# Candidates are <litellm id>[@<reasoning effort>] (effort applies to OpenAI models).
# Each model header shows elapsed time + cost of that post (usage x LiteLLM's price
# table); the file header totals cost per candidate. Output lands in eval/ (gitignored).
python -m scripts.eval_models                          # default candidate list
python -m scripts.eval_models --models anthropic/claude-sonnet-5-5,openai/gpt-6.1-sol@high
python -m scripts.eval_models --images 5 --out eval/eval_results.md

# Learning A/B (M8): same model twice, without and with the real LearningContext built
# from DATABASE_URL (point it at a prod snapshot for real feedback; read-only). The
# header records examples/edits/recent/hint counts. ~$0.60 per 10-image pair on Opus.
python -m scripts.eval_models --models anthropic/claude-opus-5-5 --images 10 --out eval/eval_plain.md
python -m scripts.eval_models --models anthropic/claude-opus-5-5 --images 10 --learning --out eval/eval_learning.md

# Render the results as a side-by-side HTML page (local, no upload; images load from
# stock/ by relative path).
python -m scripts.eval_to_html                          # eval/eval_results.md -> eval/eval_results.html
# --embed inlines downscaled images as data URIs → one self-contained file you can
# send to anyone (no stock/ folder needed on their end).
python -m scripts.eval_to_html --embed --out eval/eval_comparison_shareable.html
```

Standing task — re-run after each provider release, review natively, record the
decision in `DEV_GUIDELINES.md` ("Model decision"); if the winner changes, update
`PRODUCT_SPEC.md` §3 + `../CLAUDE.md` + `../env.example` + the config default together.
A model id LiteLLM doesn't price yet shows `cost ?` — bump `litellm` in requirements.txt.

## Quality gates

```bash
ruff check .          # lint
ruff check . --fix    # lint + autofix
mypy app              # type check
pytest                # tests (offline only — live tests skipped automatically)
pytest -v             # verbose
pytest -m live        # live integration tests (requires ANTHROPIC_API_KEY in env)
```

## Deploy to Railway (v1)

**One-time setup** (done once per service):

1. Provision a Postgres plugin → sets `${{Postgres.DATABASE_URL}}` reference var.
2. Add a persistent volume (e.g. `/data`) and create dirs on it:

   ```bash
   # SSH into the container once (railway run) or use the Railway shell:
   mkdir -p /data/stock
   # upload brand.md + stock images into /data/ (photos DM'd to the bot land there too)
   ```

3. Set Railway service variables (Settings → Variables):

   ```
   APP_ENV=production
   HOST=0.0.0.0
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   ANTHROPIC_API_KEY=<key>
   DEFAULT_LLM_MODEL=anthropic/claude-opus-5-5   # eval winner — DEV_GUIDELINES "Model decision"
   # LLM_MAX_TOKENS / LLM_REASONING_EFFORT / LEARNING_* have sane defaults — see env.example
   TELEGRAM_BOT_TOKEN=<token>
   TELEGRAM_CHAT_ID=<chat_id>
   TELEGRAM_WEBHOOK_BASE=https://<your-service>.up.railway.app
   TELEGRAM_WEBHOOK_SECRET=<random-string>
   STOCK_IMAGES_DIR=/data/stock
   BRAND_FILE=/data/brand.md
   # Schedule (change + redeploy to adjust):
   GENERATION_CRON=0 9 * * *   # when to generate suggestions (cron, in SCHEDULE_TZ)
   POSTING_SLOTS=12:00,18:00   # when to publish approved posts (HH:MM in SCHEDULE_TZ)
   SCHEDULE_TZ=Asia/Jerusalem  # IANA timezone for both (default; DST-safe)
   # Instagram (M7) — leave unset to keep the logging stub active:
   INSTAGRAM_ACCESS_TOKEN=<long-lived token — see docs/INSTAGRAM_RUNBOOK.md>
   INSTAGRAM_IG_USER_ID=<IG Business account id>
   PUBLIC_BASE_URL=https://<your-service>.up.railway.app   # usually = TELEGRAM_WEBHOOK_BASE
   ```

4. Set healthcheck path to `/health` in Railway service settings.

**Start command** (set in Railway or via `Procfile`):

```
alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

**Deploy:**

```bash
railway up       # deploy from local branch
railway logs     # stream deploy logs — confirm "alembic upgrade head" ran + "CuteBot started"
```

**Smoke test:**

```bash
# Health
curl https://<service>.up.railway.app/health  # -> {"status":"ok"}

# Webhook registered automatically on boot — confirm:
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
# -> url should show your Railway URL; pending_update_count should be 0

# Dev routes must be 404 in prod:
curl -X POST https://<service>.up.railway.app/dev/generate  # -> 404

# End-to-end: wait for generation cron (or temporarily set GENERATION_CRON=* * * * *)
# -> Telegram DM arrives → Approve → at posting slot stub-publishes once
# Restart mid-publish → recover_orphaned logs the sweep, no double-post
```

**Drift check (before/after schema changes):**

```bash
railway run alembic check  # no drift expected against models.py
```

## Database

```bash
# Tables are created on app startup (dev, via init_db()/create_all). For prod,
# Alembic owns the schema — add a migration before introducing new columns
# (see DEV_GUIDELINES: refactor responsibly).

# Generate a migration from model changes (autogenerate diffs against the DB at
# DATABASE_URL):
alembic revision --autogenerate -m "describe the change"

# Apply pending migrations:
alembic upgrade head

# Check for drift between models and the latest migration (dry-run, no DB write):
alembic check
```

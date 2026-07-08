# M4 — Ship v1 to Railway

> Implementation plan for milestone **M4** in `ROADMAP.md` — **the v1 ship**. To be
> executed in a later session. `PRODUCT_SPEC.md` is the source of truth. Predecessors:
> M0–M3 (shipped — plans in `docs/archive/`).

## Context

The loop is complete and green offline (M0–M3): image-first bilingual generation →
Telegram review → approved queue → crash-safe stub publish at fixed slots. M4 puts that
exact loop on Railway with **Postgres** and **webhook** transport, running autonomously.
Almost all of M4 is **configuration and a runbook**, not new logic — the app already has
the pieces:

- `/health` endpoint (`main.py:45`) → Railway healthcheck target.
- Alembic is set up: baseline `alembic/versions/0001_baseline.py`, `alembic/env.py` reads
  `get_settings().database_url` and runs async migrations. `init_db()`/`create_all` is
  already gated to dev only (`main.py:26`, `db.py:38`) — prod schema is Alembic's job.
- Webhook is wired: `POST /telegram/webhook` validates the secret (`main.py:58`), and
  `_set_webhook()` (`telegram.py:164`, CLI `python -m app.notifier.telegram set-webhook`)
  registers it from `telegram_webhook_base` + `telegram_webhook_secret`.
- `asyncpg` and `alembic` are already in `requirements.txt`; `app_env` gates dev routes
  and `create_all` (`config.py:59`).

**The one real code change — Postgres URL scheme.** `make_engine` (`db.py:24-25`) hands
the URL straight to `create_async_engine`. Railway's Postgres plugin injects
`DATABASE_URL` as `postgresql://…` (occasionally the legacy `postgres://`), and the async
engine **rejects both** — it requires the `postgresql+asyncpg://` driver form. Without
normalization the app crashes on boot in prod. This is the headline fix; everything else
is ops.

**Two non-code-obvious gaps (both resolved):**
1. **Migrations must run before the app serves.** Nixpacks has no Heroku-style release
   phase; chain `alembic upgrade head` into the start command. Safe because v1 is a
   **single instance** (no concurrent-migration race). (§2)
2. **Stock images + `brand.yaml` aren't in the repo** — both are gitignored owner assets
   (`.gitignore`), so a fresh container has an empty stock dir and no brand file, and
   generation produces nothing. Resolved with a **Railway persistent volume** (§4).

**Locked decisions:**
- **Single Railway service, single instance**, in-process APScheduler (no worker/Redis) —
  same as local. Scaling to >1 instance is out of scope (would need distributed locks +
  a real migration gate; see M3's `ponytail:` note on `FOR UPDATE SKIP LOCKED`).
- **Webhook, not polling, in prod.** No poller runs under uvicorn; the webhook is the
  transport. Register it automatically on prod startup (§3) so deploy stays hands-off.
- **Stub publishers still.** M4 ships the loop; the first real network is post-v1 (M-E).
- **Stock library + `brand.yaml` live on a Railway persistent volume**, not in git (§4).

## Approach

### 1. Postgres URL normalization — `app/db.py` (the one code change)
In `make_engine`, coerce a bare `postgres://` / `postgresql://` URL to
`postgresql+asyncpg://` before `create_async_engine` (leave `sqlite+aiosqlite` and an
already-correct `+asyncpg` URL untouched). One small helper, `ponytail:` — string prefix
swap, not a URL library. Leaves a self-check (`assert` in a `__main__`/tiny test) that the
three shapes (`postgres://`, `postgresql://`, `postgresql+asyncpg://`) all normalize.

### 2. Deploy config + start command — `Procfile` (or `railway.json`) + docs
- Start command runs migrations then the server, bound for the platform:
  `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
  (`$PORT` is Railway-injected; `0.0.0.0` per the container rule. `HOST`/`PORT` also map to
  `Settings.host/port` but the CLI flags are what uvicorn binds.)
- Railway healthcheck path → `/health`.
- `APP_ENV=production` so `is_dev` is False → dev routes 404, `create_all` skipped
  (Alembic owns the schema).
- **Env var inventory** (documented in `env.example` + a deploy runbook): `DATABASE_URL`
  (Postgres plugin ref), `ANTHROPIC_API_KEY`, `DEFAULT_LLM_MODEL`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `TELEGRAM_WEBHOOK_BASE` (the public Railway HTTPS URL),
  `TELEGRAM_WEBHOOK_SECRET`, `APP_ENV=production`, `STOCK_IMAGES_DIR`/`BRAND_FILE` (volume
  paths, §4). No secrets committed — all set as Railway variables.

### 3. Auto-register the webhook on prod startup — `app/main.py` lifespan
After the scheduler starts, if `not settings.is_dev` **and** `telegram_webhook_base` is
set, call `_set_webhook()` once (log the Telegram response). Makes a deploy fully
hands-off — no manual `set-webhook` step — and is idempotent (Telegram just re-points the
URL). The secret is already validated on every callback (`main.py:58`). Keep the CLI
command too for manual re-registration.

### 4. Stock library + `brand.yaml` in the container — **Railway volume (decided)**
Generation reads images from `STOCK_IMAGES_DIR` and the brand voice from `brand_file`;
both are gitignored owner assets. Mount a **Railway persistent volume**, point
`STOCK_IMAGES_DIR` and `BRAND_FILE` at a path on it, and upload the owner's real stock
images + `brand.yaml` to the volume once. The volume survives redeploys, keeps assets out
of git, and matches the "owner-provided library" model (PRODUCT_SPEC §3). Nothing new in
code — just the two env vars pointing at the mount. (Rejected: committing a sample set —
contradicts `.gitignore` and puts owner-ish assets in git.)

### 5. Deploy runbook + deployed smoke test — `SCRIPTS_REFERENCE.md` (+ short deploy note)
Document the sequence and run it once against the live service:
- Provision Postgres plugin; set env vars; deploy; confirm `alembic upgrade head` ran and
  the schema exists; `/health` returns ok (healthcheck green).
- Webhook auto-registered (or run the CLI); send a generation (wait for the cron or a
  one-off) → the Telegram DM arrives → **Approve via the real webhook** → post enqueues →
  at a posting slot it stub-publishes once with the right state transitions.
- Restart the service with a post mid-publish → `recover_orphaned` logs the sweep on boot
  (M3) → no double-post.
- Consider the `deploy-check` skill as the pre-deploy gate.

## Files
- Modify: `app/db.py` (URL normalization + self-check), `app/main.py` (prod webhook
  auto-register in lifespan), `env.example` (prod var inventory + volume paths),
  `SCRIPTS_REFERENCE.md` (deploy runbook), `.gitignore`/README if §4 needs a note.
- Add: `Procfile` (or `railway.json`/`railway.toml`) with the migrate-then-serve start
  command + healthcheck.
- **No new DB column ⇒ no new migration.** (The baseline `0001_baseline.py` is the prod
  schema; verify it applies clean on Postgres with no drift vs the models.)

## Verification
1. `ruff check . && mypy app && pytest` — green (URL-normalization self-check included).
2. **Local Postgres dry run** (Docker or a scratch Railway DB): set `DATABASE_URL` to a
   `postgresql://` URL, boot the app → it normalizes to `+asyncpg`, `alembic upgrade head`
   builds the schema, `alembic check` reports no drift vs `models.py`.
3. **Deployed:** `/health` green; webhook registered (Telegram `getWebhookInfo` shows the
   Railway URL + secret); a real Approve round-trips through `/telegram/webhook`; a posting
   slot stub-publishes an approved post once; restart mid-publish self-heals.
4. Dev routes (`/dev/*`) return 404 in prod (`APP_ENV=production`).

**DoD:** the full loop runs autonomously in production on Railway + Postgres with stub
publishers, webhook review, and migrations applied on deploy — **v1 shipped.**

## Out of scope (post-v1)
Real network publishers + OAuth (M-E); horizontal scaling / multi-instance (distributed
locks, migration gating, `FOR UPDATE SKIP LOCKED`); autoscaling, blue-green deploys,
metrics/alerting dashboards; multi-tenant `brand_id` scoping (post-v1 G). No CI/CD pipeline
changes beyond Railway's push-to-deploy unless a real need appears.

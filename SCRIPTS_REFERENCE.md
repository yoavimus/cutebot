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

Dev-only endpoints, enabled when `APP_ENV=development`:

```bash
# Generate a batch now and DM the suggestions to Telegram
curl -s -X POST http://127.0.0.1:8002/dev/generate

# Drain one post from the queue to the publishers now
curl -s -X POST http://127.0.0.1:8002/dev/publish-next
```

## Telegram review channel

```bash
# Long-polling (no public URL needed) — runs the bot loop locally:
python -m app.notifier.telegram poll

# Webhook mode (needs TELEGRAM_WEBHOOK_BASE set to a public HTTPS URL):
python -m app.notifier.telegram set-webhook
python -m app.notifier.telegram delete-webhook
```

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
   # scp or upload brand.yaml + stock images into /data/
   ```

3. Set Railway service variables (Settings → Variables):

   ```
   APP_ENV=production
   HOST=0.0.0.0
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   ANTHROPIC_API_KEY=<key>
   DEFAULT_LLM_MODEL=anthropic/claude-sonnet-4-6
   TELEGRAM_BOT_TOKEN=<token>
   TELEGRAM_CHAT_ID=<chat_id>
   TELEGRAM_WEBHOOK_BASE=https://<your-service>.up.railway.app
   TELEGRAM_WEBHOOK_SECRET=<random-string>
   STOCK_IMAGES_DIR=/data/stock
   BRAND_FILE=/data/brand.yaml
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

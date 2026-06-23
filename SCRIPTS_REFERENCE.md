# CuteBot — Scripts Reference

> **Shell**: zsh on WSL2 (Linux). `&&` chaining works; `curl` is real curl.
> **Project root**: `/home/yoav/Projects/cutebot`
> Keep this file in sync — every reusable command/script lives here.

## Environment

```bash
python3 -m venv .venv           # create once
source .venv/bin/activate       # activate (prompt shows (.venv))
pip install -r requirements.txt # install deps
cp env.example .env             # then fill in secrets
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
pytest                # tests
pytest -v             # verbose
```

## Database

```bash
# Tables are created on app startup (dev). For prod, add an Alembic migration
# before introducing new columns (see DEV_GUIDELINES: refactor responsibly).
```

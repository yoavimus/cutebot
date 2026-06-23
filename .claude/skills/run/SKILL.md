---
name: run
description: Launch and verify CuteBot locally — FastAPI app + in-process scheduler (:8002) and the Telegram review bot
---

## CuteBot local startup

### Prerequisites — `.env`

`.env` must exist. If missing, create it from the template and fill in secrets:

```bash
cp env.example .env
# Required to do anything useful:
#   ANTHROPIC_API_KEY   (generation)
#   DATABASE_URL        (Postgres, or sqlite+aiosqlite:///./cutebot.db for quick local)
#   TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID  (review loop)
```

**Gotchas:**
- Port **8002** (8000/8001 are taken by other local projects). Kill stale procs first:
  `lsof -ti:8002 | xargs kill -9 2>/dev/null`
- For zero-setup local dev use SQLite: `DATABASE_URL=sqlite+aiosqlite:///./cutebot.db`.
  Tables are auto-created on startup in `APP_ENV=development`.
- `POSTING_SLOTS` and `GENERATION_CRON` drive the scheduler; the app logs the jobs it
  registered at startup — confirm they appear.

### Start the app (API + scheduler)

```bash
lsof -ti:8002 | xargs kill -9 2>/dev/null; sleep 1
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload &
```

Verify: `curl -s http://127.0.0.1:8002/health` → `{"status":"ok"}`

### Drive the pipeline without waiting for cron (dev endpoints)

```bash
curl -s -X POST http://127.0.0.1:8002/dev/generate       # generate a batch + DM suggestions
curl -s -X POST http://127.0.0.1:8002/dev/publish-next   # publish the front of the queue now
```

### Telegram review bot

Long-polling (no public URL; easiest locally):

```bash
python -m app.notifier.telegram poll
```

Webhook mode (needs `TELEGRAM_WEBHOOK_BASE` = public HTTPS base, e.g. ngrok/Railway):

```bash
python -m app.notifier.telegram set-webhook
```

Tap **Approve** / **Reject** on a suggestion in your chat and confirm the post's
status changes (`approved`/`rejected`) and a `Feedback` row is written.

### Run tests

```bash
pytest -v
```

Tests use an in-memory SQLite DB (no Postgres or network required).

### Quality gates before committing

```bash
ruff check . && mypy app && pytest
```

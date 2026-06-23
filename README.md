# CuteBot 🤖

**An autonomous, human-in-the-loop social-media content pipeline for small brands.**

CuteBot acts as a background brand copywriter and coordinator: on a schedule it
drafts a batch of on-brand post suggestions (caption + visual concept), sends each
one to your personal chat app (Telegram) with **Approve / Reject** buttons, queues
the approved ones, and publishes them to your linked social networks at your brand's
pre-set posting times. Every decision you make is saved as feedback that sharpens
future suggestions.

> **You stay in control of schedule and quality.** Nothing is published without your
> explicit approval, and posts only go out at your configured time slots.

## The four-step cycle

1. **Batch generation** — a scheduled job reads your brand guidelines and generates
   N post suggestions, each with a caption written in your style and a matching
   visual concept. (`app/pipeline/generate.py`)
2. **Review & feedback loop** — each suggestion is DM'd to you via Telegram with
   inline `Approve` / `Reject` buttons. Your choice is persisted as training data.
   (`app/pipeline/review.py`, `app/notifier/telegram.py`)
3. **Queue** — approved posts enter an ordered content queue, held until a posting
   slot arrives. (`app/pipeline/queue.py`)
4. **Scheduled publishing** — at each configured slot, the next approved post is
   pulled from the front of the queue and broadcast to every linked network.
   (`app/pipeline/publish.py`, `app/publishers/`)

## Stack

- **Backend / pipeline**: Python 3.12 · FastAPI · async SQLAlchemy
- **Scheduler**: APScheduler (in-process; no Redis/worker)
- **AI**: a single LiteLLM function-calling agent, **Claude by default**
  (`DEFAULT_LLM_MODEL=anthropic/claude-sonnet-4-6`) — one-env-var switch to any provider
- **Review channel**: Telegram bot (inline buttons); notifier interface is
  platform-agnostic so Discord/Slack can be added as adapters
- **Publishers**: pluggable per-network adapters (Instagram / TikTok / X) behind one
  interface; ship as stubs in v1
- **Database**: PostgreSQL (async)
- **Deployment**: any container host (Railway-friendly)

`PRODUCT_SPEC.md` is the **source of truth** for scope, architecture, and roadmap.

## Quickstart

```bash
# 1. Install deps (uses a virtualenv)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env       # then fill in ANTHROPIC_API_KEY, DATABASE_URL, TELEGRAM_*

# 3. Run the API + scheduler
uvicorn app.main:app --reload --host 127.0.0.1 --port 8002
```

Health check: `curl -s http://127.0.0.1:8002/health` → `{"status":"ok"}`.

See the `run` Claude skill (`.claude/skills/run/SKILL.md`) for the full local
startup, including Telegram webhook setup, or `SCRIPTS_REFERENCE.md` for the script
inventory.

## Status

🚧 **Skeleton / scaffold.** The pipeline stages, models, scheduler, and Telegram
notifier are wired with clear extension points; LLM generation and social publishing
are functional stubs ready to be filled in. See `PRODUCT_SPEC.md` §Roadmap.

## License

MIT — see [LICENSE](LICENSE).

---

🤖 Built with [Claude Code](https://claude.com/claude-code).

# CuteBot — Project-level Claude Instructions

## Product

CuteBot — an autonomous, **human-in-the-loop social-media content pipeline** for small
brands. On a schedule it drafts on-brand posts, DMs each to the owner via Telegram for
one-tap Approve/Reject, queues approvals, and publishes to linked networks at fixed
slots — learning from every decision. **`PRODUCT_SPEC.md` is the source of truth** for
scope, architecture, and roadmap.

v1 = the four-stage cycle (generate → review → queue → publish) with a single LLM
agent, a Telegram review channel, and stubbed network publishers. Deferred to the
roadmap (PRODUCT_SPEC §8): learning-loop v2, image generation, inline editing, more
review channels, real publishers, analytics, multi-tenant.

## AI / model strategy (runtime)

- The pipeline's **runtime** LLM is **Claude via LiteLLM**, set by `DEFAULT_LLM_MODEL`
  (default `anthropic/claude-sonnet-4-6` — cost-effective for high-volume copywriting;
  switch to `anthropic/claude-opus-4-8` for max quality). One-env-var provider switch.
- All model calls go through `app/llm.py` — never call LiteLLM/Anthropic SDK directly
  from pipeline code. The agent returns **structured** post objects (Pydantic), never
  free-text to be parsed.
- This is separate from the Claude model used to *develop* CuteBot.
- When touching LLM code, consult the `claude-api` skill for current model IDs and API
  shape — do not answer model questions from memory.

## Cognitive Workflow (RPER)

Defined in global `~/.claude/CLAUDE.md` — applies here. Every non-trivial task:
**Research → Plan → Execute → Reflect** (run lint + tests; delete throwaway files).
Agent completion messages MUST include Research/Plan/Changes/Reflect sections, and
non-obvious findings MUST be persisted to memory (project or global). Never skip the
memory step.

## Developer guardrails (see DEV_GUIDELINES.md)

- **Approval gate is load-bearing** — the publish path may only act on `approved`
  posts. Never auto-approve, never publish from `suggested`.
- **Notifier & Publisher are interfaces** — add new channels/networks as adapters
  behind `app/notifier/base.py` / `app/publishers/base.py`; don't special-case.
- **Structured data over parsing** — LLM output is a validated schema.
- **No secrets in code or logs.** All config via env (`app/config.py`).
- **Refactor responsibly** — DB shape change ⇒ migration; new dep ⇒ update
  `requirements.txt` *and* `pyproject.toml`. New script ⇒ update `SCRIPTS_REFERENCE.md`.
- **Test before moving on** — `ruff check . && mypy app && pytest`.

## Local dev (WSL2 / Linux, zsh)

- **Project root**: `/home/yoav/Projects/cutebot`.
- **Port 8002** (8000/8001 are taken by other local projects).
- **Virtual env**: `source .venv/bin/activate` (look for `(.venv)` prefix).
- **FastAPI local**: bind `127.0.0.1`; use `0.0.0.0` for containers/Railway.
- See `.claude/skills/run/SKILL.md` for full startup incl. Telegram webhook/polling.

## Code quality checklist

- `ruff check . && mypy app` — lint + type check
- `pytest` — tests pass
- App boots without errors; scheduler registers jobs
- Approve/Reject round-trip works against a test Telegram chat
- No secrets committed

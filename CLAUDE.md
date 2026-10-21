# CuteBot — Project-level Claude Instructions

## Product

CuteBot — an autonomous, **human-in-the-loop social-media content pipeline** for small
brands. On a schedule it drafts on-brand posts, DMs each to the owner via Telegram for
one-tap Approve/Reject, queues approvals, and publishes to linked networks at fixed
slots — learning from every decision. **`docs/PRODUCT_SPEC.md` is the source of truth**
for scope, architecture, and roadmap.

Shipped: the four-stage cycle in prod (Railway), Telegram operator console, Instagram
publisher, learning loop v1, bot console v2 (Keep/Almost/Retry verdicts, `/mode`,
`/schedule`, slash menu + HTML) and the tenant seam (M10: `brands` table, `brand_id`
everywhere, brand text in the DB with `brand.md` as seed, cost per post) and media v2 (M11:
Telegram files + albums, `/stock` thumbnail browser, Google Drive folder sync with
posted/banned write-back, `stock_images` provenance) — M0–M11. Next (2026-10-14 order):
anchors → beta onboarding → IG DM assistant → analytics → limits; carousels as filler — `docs/ROADMAP.md`. Instagram-only until a brand asks otherwise.

## Docs layout

All project docs live under `docs/` (only `README.md`, this file and tool configs stay
at the root): `PRODUCT_SPEC.md` (truth), `ROADMAP.md` (sequencing), `DEV_GUIDELINES.md`
(rules + model-decision log), `SCRIPTS_REFERENCE.md` (every command),
`INSTAGRAM_RUNBOOK.md`, `brand.example.md`, `plans/` (active), `archive/` (shipped),
`issues/` (owner feedback). Bare names like "DEV_GUIDELINES" elsewhere mean these files.

## AI / model strategy (runtime)

- The pipeline's **runtime** LLM goes through **LiteLLM**, set by `DEFAULT_LLM_MODEL`
  (default `anthropic/claude-opus-5-5` — won the 2026-10 native-Hebrew eval over Sonnet
  5.5 and GPT-6.1 Sol; see DEV_GUIDELINES "Model decision"). One-env-var provider
  switch; the choice is re-evaluated with `scripts/eval_models.py` after each provider
  release.
- All model calls go through `app/llm.py` — never call LiteLLM/provider SDKs directly
  from pipeline code. The agent returns **structured** post objects (Pydantic), never
  free-text to be parsed.
- This is separate from the Claude model used to *develop* CuteBot.
- When touching LLM code, consult the `claude-api` skill for current model IDs and API
  shape — do not answer model questions from memory.

## Linear (CUT- tickets)

- **Never create milestone tickets before proving none exist.** Search with
  `list_issues(team="CuteBot", query="M6")` and **no `state` filter** — new tickets
  land in **Backlog**, so a `state: "Todo"` filter returns empty and has already
  caused a duplicate ticket set (M6, 2026-07-08).
- The active milestone plan (`docs/plans/<ticket>.md`; shipped plans move to
  `docs/archive/M<N>_PLAN.md`) lists its ticket IDs in the header — those are canonical;
  implement against them. Owner issue lists + findings live in `docs/issues/`.
- When creating tickets, set `state: "Todo"` explicitly so default-state mismatches
  can't hide them.

## Cognitive Workflow (RPER)

Defined in global `~/.claude/CLAUDE.md` — applies here. Every non-trivial task:
**Research → Plan → Execute → Reflect** (run lint + tests; delete throwaway files).
Agent completion messages MUST include Research/Plan/Changes/Reflect sections, and
non-obvious findings MUST be persisted to memory (project or global). Never skip the
memory step.

## Developer guardrails (see docs/DEV_GUIDELINES.md)

- **Approval gate is load-bearing** — the publish path may only act on `approved`
  posts. Never auto-approve, never publish from `suggested`.
- **Notifier & Publisher are interfaces** — add new channels/networks as adapters
  behind `app/notifier/base.py` / `app/publishers/base.py`; don't special-case.
- **Structured data over parsing** — LLM output is a validated schema.
- **No secrets in code or logs.** All config via env (`app/config.py`).
- **Refactor responsibly** — DB shape change ⇒ migration; new dep ⇒ update
  `requirements.txt` *and* `pyproject.toml`. New script ⇒ update `docs/SCRIPTS_REFERENCE.md`.
- **Test before moving on** — `ruff check . && mypy app && pytest`.

## Local dev (WSL2 / Linux, zsh)

- **Project root**: `/home/yoav/Projects/cutebot`.
- **Port 8002** (8000/8001 are taken by other local projects).
- **Python env**: per the global standard — `uv` with a pinned interpreter. Target is
  **Python 3.12** (`.python-version`); `uv venv && source .venv/bin/activate`. Don't use
  a bare `python3 -m venv` (the machine's `python3` is 3.14 and breaks 3.12-only wheels).
- **FastAPI local**: bind `127.0.0.1`; use `0.0.0.0` for containers/Railway.
- See `.claude/skills/run/SKILL.md` for full startup incl. Telegram webhook/polling.

## Code quality checklist

- `ruff check . && mypy app` — lint + type check
- `pytest` — tests pass
- App boots without errors; scheduler registers jobs
- Approve/Reject round-trip works against a test Telegram chat
- No secrets committed

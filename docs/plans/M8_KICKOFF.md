# M8 kickoff prompt

Paste everything below the line into a fresh Claude Code session (Sonnet) started in
`~/Projects/cutebot`. It is written to run unattended until the M8 tickets are done.

---

You are implementing milestone **M8 — Learning loop v1** for CuteBot, end to end, without
checking in with me. Work until every ticket is done or genuinely blocked, then stop with
a summary.

## Read first (in this order)

1. `CLAUDE.md` and `DEV_GUIDELINES.md` — project rules (approval gate is load-bearing;
   structured LLM output; migrations for DB changes; `ruff check . && mypy app && pytest`
   before moving on).
2. `docs/plans/M8_PLAN.md` — the approved plan. It is canonical for scope and design.
3. Linear tickets **CUT-64, CUT-65, CUT-66, CUT-67** (epic CUT-63) — the deliverable
   checklists. Use the Linear MCP tools; team "CuteBot".
4. The code the plan names: `app/llm.py`, `app/pipeline/generate.py`,
   `app/pipeline/review.py`, `app/notifier/telegram.py`, `app/models.py`, `app/stock.py`
   (the `banned_images` + `/stock` work from 2026-10-05 is the style to match),
   `alembic/versions/0005_banned_images.py`, `scripts/eval_models.py`, `tests/`.

## Environment

- Python env: `source .venv/bin/activate` (uv-managed, Python 3.12). Never use the system
  python. `litellm==1.104.0` is installed.
- `.env` exists with `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`; runtime model is
  `anthropic/claude-opus-5-5`. Tests are fully offline (LLM is stubbed in fixtures).
- Local `cutebot.db` is a stale pre-M7 SQLite file. If you need a local DB, move it to
  `~/trash/` (`mkdir -p ~/trash && mv cutebot.db ~/trash/cutebot-$(date +%F-%H%M%S).db`)
  and let `init_db` recreate it on boot. Never `rm -rf` anything.
- `alembic upgrade head` does not work on SQLite (migration 0002 is Postgres-only);
  verify migrations via the test suite (`create_all`) and by reading the migration.
- Long commands (pytest, evals) run in the background; don't sleep in the foreground.

## Working rules

- Branch: `git checkout -b m8-learning-loop` from `main` (pull first). Small, focused
  commits per ticket, message ends with
  `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Order: CUT-64 and CUT-65 are independent — do CUT-64 first, then CUT-65, then CUT-66.
  CUT-67 only if the first three are done and green.
- Linear: set each ticket **In Progress** when you start it and **Done** with a short
  comment (what shipped, commit hash) when it's green. Never create new tickets; if you
  find work outside the plan, note it in the final summary instead.
- Follow the plan's design decisions. If something in the plan turns out to be wrong
  against the code, pick the smallest change that keeps the plan's intent, add a
  `# ponytail:` comment where you simplified, and say so in the summary.
- Every non-trivial branch/loop/parser gets a test. Keep the suite offline.
- Docs are part of the work: `SCRIPTS_REFERENCE.md` (bot commands, eval flag),
  `env.example` (new vars), `ROADMAP.md` (tick M8 items), `PRODUCT_SPEC.md` §8 (A and C
  → shipped). On CUT-66 move `docs/plans/M8_PLAN.md` to `docs/archive/M8_PLAN.md`.
- Paid API calls: allowed for the CUT-66 A/B only, budget **$2 total**. Run the A/B only
  if real feedback data is reachable (a Postgres `DATABASE_URL` with approved posts — check
  `railway variables` for a public URL; read-only use). If it isn't, build the `--learning`
  flag, test it against a seeded in-memory DB, and leave the paid run for me — say so.
- Telegram: do not send messages to the real chat. Verify flows with the offline tests
  (`tests/test_telegram.py` has the `_FakeNotifier` pattern).

## Hard gates (stop and leave for me)

- **Do not merge to `main`.** Merging auto-deploys to Railway and runs migration 0006
  against the production DB. When everything is green: `git push -u origin
  m8-learning-loop`, open a PR with `gh pr create` (body = per-ticket summary + the
  verification you ran + deploy notes), and stop.
- Do not touch Railway (`railway up`, variables, scaling).
- Do not edit `brand.md` (gitignored, the owner's live file) — CUT-67 only *proposes* a
  diff and writes the file after an explicit ✅ in Telegram; tests use a temp file.

## Definition of done for this session

- CUT-64, CUT-65, CUT-66 Done in Linear (CUT-67 Done or left Todo with a note).
- `ruff check . && mypy app && pytest` green; app boots against a fresh SQLite DB
  (`/health` 200, scheduler jobs registered — see the boot check in
  `docs/issues/cutebot_issues_05_10_2026_findings.md` era commits, or just run uvicorn
  briefly).
- Branch pushed, PR open, not merged.
- Final message: Research / Plan / Changes / Reflect sections, per-ticket status, any
  deviations from the plan, the PR link, and non-obvious findings (save those to
  `~/.claude/projects/-home-yoav-Projects-cutebot/memory/` or global memory per
  `CLAUDE.md` — this is mandatory, not optional).

Start now with the reading list, then CUT-64.

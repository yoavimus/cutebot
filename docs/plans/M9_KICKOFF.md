# M9 kickoff prompt

Paste everything below the line into a fresh Claude Code session (Sonnet) started in
`~/Projects/cutebot`. It is written to run unattended until the M9 tickets are done.
Template: `docs/archive/M8_KICKOFF.md`. Archive this file with the plan when M9 ships.

---

You are implementing milestone **M9 — Bot console v2** for CuteBot, end to end, without
checking in with me. Work until every ticket is done or genuinely blocked, then stop with
a summary.

## Read first (in this order)

1. `CLAUDE.md` and `docs/DEV_GUIDELINES.md` — project rules (approval gate is
   load-bearing; structured LLM output; migrations for DB changes; lint + types + tests
   before moving on).
2. `docs/plans/M9_PLAN.md` — the approved plan. Canonical for scope and design; the
   "Locked decisions" block is not up for debate.
3. Linear tickets **CUT-69, CUT-70, CUT-71** (epic **CUT-68**, which also holds the
   close-out checklist) — the deliverable checklists. Linear MCP tools, team "CuteBot".
4. The code the plan names: `app/models.py`, `app/pipeline/review.py`,
   `app/pipeline/queue.py`, `app/pipeline/publish.py`, `app/learning.py`,
   `app/scheduler.py`, `app/main.py`, `app/notifier/telegram.py`, `app/stock.py`,
   `app/config.py`, `alembic/versions/0006_*.py` (latest migration — match its style),
   `tests/` (`tests/test_telegram.py` has the `_FakeNotifier` pattern).

## Environment

- Python env: `source .venv/bin/activate` (uv-managed, Python 3.12). Never use the system
  python. Gates are the pinned tools: `.venv/bin/ruff check . && .venv/bin/mypy app &&
  .venv/bin/pytest` (`uvx ruff` is a newer version and reports noise that is not a gate).
- `.env` exists; runtime model is `anthropic/claude-opus-5-5`. Tests are fully offline
  (LLM stubbed in fixtures). **No paid API calls this milestone.**
- No local `cutebot.db` exists; `init_db` creates one on boot if you run the app. If one
  appears stale, move it to `~/trash/` (`mkdir -p ~/trash && mv cutebot.db
  ~/trash/cutebot-$(date +%F-%H%M%S).db`). Never `rm -rf` anything.
- `alembic upgrade head` does not work on SQLite (migration 0002 is Postgres-only);
  verify 0007 and 0008 via the test suite (`create_all`) and by reading them.
- Long commands (pytest, uvicorn) run in the background; don't sleep in the foreground.

## Working rules

- Branch **`m9-console` already exists locally** with the plan commits: `git checkout
  m9-console` (do not branch from `main`). Small, focused commits per ticket, message
  ends with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Order is fixed: **CUT-69 → CUT-70 → CUT-71.** 69 and 70 both change
  `review.handle_decision` and the card keyboards; 71 formats what 69 and 70 add.
- Linear: set each ticket **In Progress** when you start it and **Done** with a short
  comment (what shipped, commit hash) when it's green. Never create new tickets; if you
  find work outside the plan, note it in the final summary instead.
- Follow the plan's design decisions. If something in the plan is wrong against the
  code, pick the smallest change that keeps the plan's intent, add a `# ponytail:`
  comment where you simplified, and say so in the summary.
- Every non-trivial branch/loop/parser gets a test. Keep the suite offline.
- Docs are part of the work and live on the epic's close-out checklist (CUT-68):
  `docs/SCRIPTS_REFERENCE.md` bot section, `docs/PRODUCT_SPEC.md` (lifecycle `kept`,
  Feedback `almost`, `settings` table), `docs/ROADMAP.md` M9 row, `env.example`
  (cron/slots are overridable defaults), `docs/DEV_GUIDELINES.md` lessons, then move
  `docs/plans/M9_PLAN.md` and `docs/plans/M9_KICKOFF.md` to `docs/archive/` and update
  the summary line in `CLAUDE.md`. Tick the boxes on CUT-68 as you go.
- Telegram: do not send messages to the real chat. Verify flows with the offline tests.
  Menu labels: plain text, no emoji (owner decision). No reply keyboard.

## Hard gates (stop and leave for me)

- **Do not merge to `main`.** Merging auto-deploys to Railway and runs migrations 0007
  and 0008 against the production DB. When everything is green: `git push -u origin
  m9-console`, open a PR with `gh pr create` (body = per-ticket summary + the
  verification you ran + deploy notes incl. "after deploy run `/mode learning`"), and stop.
- Do not touch Railway (`railway up`, variables, scaling).
- Do not edit `brand.md` or `.env`.

## Definition of done for this session

- CUT-69, CUT-70, CUT-71 Done in Linear; CUT-68 checklist ticked except the owner-side
  item.
- `.venv/bin/ruff check . && .venv/bin/mypy app && .venv/bin/pytest` green; app boots
  against a fresh SQLite DB (`/health` 200, scheduler jobs registered, stored mode
  applied — run uvicorn briefly on port 8002 and read the log).
- Branch pushed, PR open, not merged.
- Final message: Research / Plan / Changes / Reflect sections, per-ticket status, any
  deviations from the plan, the PR link, and non-obvious findings saved to
  `~/.claude/projects/-home-yoav-Projects-cutebot/memory/` (or global memory per
  `CLAUDE.md`) — mandatory, not optional.

Start now with the reading list, then CUT-69.

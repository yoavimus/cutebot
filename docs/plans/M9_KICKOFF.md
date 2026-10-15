# M9 kickoff prompts — three sessions, one per ticket

The tickets are sequential (each edits `review.handle_decision` / `telegram.py` on top
of the previous one), so the sessions run **one after another on the same branch**, not
in parallel. Start session N+1 only after session N reports Done and its commit is on
`m9-console`. One laptop, one checkout: **no worktrees, no extra branches** — every
session works in `~/Projects/cutebot` on `m9-console`, one session at a time.

| Session | Ticket | Model | Why |
|---------|--------|-------|-----|
| 1 | CUT-69 M9.1 verdicts | **Opus 5.5** | Cross-cutting: data model + migration, the review state machine next to the load-bearing approval gate, learning-context rendering, stock exclusion, four new callbacks. Most places to get subtly wrong. |
| 2 | CUT-70 M9.2 mode + schedule | **Sonnet 5.5** | Contained: new table + `prefs.py`, APScheduler pause/resume/reschedule, fail-closed boot. Same size as the M6/M8 work Sonnet shipped. |
| 3 | CUT-71 M9.3 console UX + close-out | **Sonnet 5.5** | Single file, presentation + tests, then the docs checklist and the PR. Haiku would manage the menu table but not reliably the escaping/callback tests; the saving isn't worth a second pass. |

For each session: start a fresh Claude Code session in `~/Projects/cutebot` with
`/model` set as above, paste the **Common block** and then that session's block.

---

## Common block (paste first, every session)

You are implementing part of milestone **M9 — Bot console v2** for CuteBot without
checking in with me. Work until your ticket is done or genuinely blocked, then stop with
a summary.

### Read first (in this order)

1. `CLAUDE.md` and `docs/DEV_GUIDELINES.md` — project rules (approval gate is
   load-bearing; structured LLM output; migrations for DB changes; lint + types + tests
   before moving on).
2. `docs/plans/M9_PLAN.md` — the approved plan. Canonical for scope and design; the
   "Locked decisions" block is not up for debate. Read all of it, not only your section:
   the other tickets explain the shape your code must leave behind.
3. Your Linear ticket (named in your session block) and the epic **CUT-68**. Linear MCP
   tools, team "CuteBot".
4. `git log --oneline main..m9-console` — what the earlier sessions already shipped.

### Environment

- Python env: `source .venv/bin/activate` (uv-managed, Python 3.12). Never the system
  python. Gates are the pinned tools: `.venv/bin/ruff check . && .venv/bin/mypy app &&
  .venv/bin/pytest` (`uvx ruff` is newer and reports noise that is not a gate).
- `.env` exists; runtime model is `anthropic/claude-opus-5-5`. Tests are fully offline
  (LLM stubbed in fixtures). **No paid API calls this milestone.**
- No local `cutebot.db`; `init_db` creates one on boot if you run the app. If one is
  stale, move it to `~/trash/` (`mkdir -p ~/trash && mv cutebot.db
  ~/trash/cutebot-$(date +%F-%H%M%S).db`). Never `rm -rf` anything.
- `alembic upgrade head` does not work on SQLite (migration 0002 is Postgres-only);
  verify new migrations via the test suite (`create_all`) and by reading them.
- Long commands (pytest, uvicorn) run in the background; don't sleep in the foreground.

### Working rules

- Branch **`m9-console` exists locally**: `git checkout m9-console` (never branch from
  `main`, never rebase, never create a worktree or another branch — `EnterWorktree` is
  off limits; this checkout is the only one). Small, focused commits; message ends with
  `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- Linear: set your ticket **In Progress** when you start and **Done** with a short
  comment (what shipped, commit hash) when it's green. Never create tickets; work you
  find outside the plan goes in the final summary.
- Follow the plan. If it is wrong against the code, pick the smallest change that keeps
  its intent, add a `# ponytail:` comment where you simplified, and say so in the
  summary. Only touch what your ticket names.
- Every non-trivial branch/loop/parser gets a test. Keep the suite offline.
- Telegram: do not send messages to the real chat; verify with the offline tests
  (`tests/test_telegram.py`, `_FakeNotifier`). Menu labels: plain text, no emoji. No
  reply keyboard.

### Hard gates (stop and leave for me)

- **Do not merge to `main`** (auto-deploys to Railway and runs migrations 0007/0008 on
  the production DB). **Do not push** unless your session block says so.
- Do not touch Railway (`railway up`, variables, scaling). Do not edit `brand.md` or
  `.env`.

### Final message

Research / Plan / Changes / Reflect sections, ticket status, deviations from the plan,
and non-obvious findings saved to `~/.claude/projects/-home-yoav-Projects-cutebot/memory/`
(or global memory per `CLAUDE.md`) — mandatory, not optional.

---

## Session 1 — CUT-69 · M9.1 verdicts · Opus 5.5

Your ticket is **CUT-69**: `PostStatus.KEPT`, `Decision.ALMOST`, `Post.retry_of` +
migration `0007_verdicts`, `handle_decision(keep | almost)` with the reversal rules,
`review.retry()`, learning `near_misses` + retry pairs + kept as examples, the two-row
card keyboard and the `keep:` / `almost:` / `almostr:` / `retry:` callbacks, the stock
hold while a retry is pending, `/postnow` refusing kept posts.

Leave behind for session 2: `handle_decision` must accept `decision="keep"` as a plain
argument (session 2 makes ✅ call it in learning mode), and `/status` must have one
place that renders the learning line (session 2 prepends a mode header to it).

Done = ticket checklist green, gate green, app boots against a fresh SQLite DB, one or
more commits on `m9-console`, CUT-69 Done in Linear. **Commit and stop; do not push.**

---

## Session 2 — CUT-70 · M9.2 mode + schedule · Sonnet 5.5

Prerequisite: CUT-69 is Done and `git log --oneline main..m9-console` shows its
commits. If not, stop and say so.

Your ticket is **CUT-70**: `settings` table + migration `0008_settings`, `app/prefs.py`,
`/mode learning|live` (✅ → Keep via `handle_decision(keep)`, slot jobs paused, `/postnow`
refuses, `/status` header, **fail closed at boot**), `/schedule` view / `post` /
`generate` / `reset` with `apply_slots()` extracted from `build_scheduler` and
`reschedule_job("generation")`, catch-up and generation reading `prefs` not `Settings`.
A typed `/mode learning` is enough — the confirmation keyboard is session 3's.

Leave behind for session 3: `/mode` with no argument returns the current mode as plain
text; `prefs.mode()` is the single read used by review, publish and status.

Done = ticket checklist green, gate green, app boots against a fresh SQLite DB and
applies the stored mode (run uvicorn briefly on port 8002, read the log), commits on
`m9-console`, CUT-70 Done in Linear. **Commit and stop; do not push.**

---

## Session 3 — CUT-71 · M9.3 console UX + M9 close-out · Sonnet 5.5

Prerequisite: CUT-69 and CUT-70 are Done and on `m9-console`. If not, stop and say so.

Your ticket is **CUT-71**: `_COMMANDS` table driving `setMyCommands` (plain labels ≤ 32
chars, owner-chat scope with default-scope fallback, `requeue` out of the menu) and the
grouped `/help`; `send_message(text, *, html=False, keyboard=None)`; HTML `/status`,
`/queue` rows with `postnow:<id>` / `drop:<id>`, `/stock` rows with `unban:<ref>` (64-byte
cap → text fallback), `/pending` count line, `/mode` confirmation keyboard
(`mode:learning` / `mode:live` / cancel).

Then the **close-out checklist on CUT-68** (tick each box): `docs/SCRIPTS_REFERENCE.md`
bot section, `docs/PRODUCT_SPEC.md` (lifecycle `kept`, Feedback `almost`, `settings`
table), `docs/ROADMAP.md` M9 row, `env.example` (cron/slots are overridable defaults),
`docs/DEV_GUIDELINES.md` lessons, move `docs/plans/M9_PLAN.md` and
`docs/plans/M9_KICKOFF.md` to `docs/archive/`, update the summary line in `CLAUDE.md`.
Leave the owner-side item unticked.

Done = ticket checklist green, gate green, app boots against a fresh SQLite DB, CUT-71
Done and CUT-68 ticked. **Then, and only then:** `git push -u origin m9-console`, open a
PR with `gh pr create` (body = per-ticket summary for 69/70/71 + the verification you
ran + deploy notes incl. "after deploy run `/mode learning`"), and stop. Do not merge.

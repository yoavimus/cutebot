# M10 kickoff prompts — two sessions, split by model

The three tickets share files (`models.py`, `generate.py`, `telegram.py`, migrations), so
they run **one after another on the same branch**. Two sessions, not three: a session
boundary only pays for itself when the model changes. One laptop, one checkout: **no
worktrees, no extra branches** — every session works in `~/Projects/cutebot` on
`m10-tenant`, one session at a time.

| Session | Tickets | Model | Why |
|---------|---------|-------|-----|
| 1 | CUT-74 M10.1 brand row + `brand_id` seam | **Opus 5.5** | Migration with backfill on live data and a seam through every module. A wrong assumption here costs a day. |
| 2 | CUT-75 M10.2 brand text in DB, CUT-76 M10.3 cost, close-out | **Sonnet 5.5** | Mechanical once the seam exists: distill on the row, a sync script, two columns, docs, PR. |

For each session: start a fresh Claude Code session in `~/Projects/cutebot` with
`/model` set as above, paste the **Common block** and then that session's block.

---

## Common block (paste first, every session)

You are implementing part of milestone **M10 — Tenant seam** for CuteBot without
checking in with me. Work until your tickets are done or genuinely blocked, then stop
with a summary.

### Read first (in this order)

1. `CLAUDE.md` and `docs/DEV_GUIDELINES.md` — project rules (approval gate is
   load-bearing; structured LLM output; migrations for DB changes; lint + types + tests
   before moving on).
2. `docs/plans/M10_PLAN.md` — the approved plan. Canonical for scope and design; the
   decisions under "Risks and decisions" were confirmed on 2026-10-21 and are not up for
   debate. Read all of it, not only your section.
3. Your Linear tickets (named in your session block) and the epic **CUT-73**. Linear MCP
   tools, team "CuteBot".
4. `git log --oneline main..m10-tenant` — what the earlier session already shipped.

### Environment

- Python env: `source .venv/bin/activate` (uv-managed, Python 3.12). Never the system
  python. Gates are the pinned tools: `.venv/bin/ruff check . && .venv/bin/mypy app &&
  .venv/bin/pytest` (`uvx ruff` is newer and reports noise that is not a gate).
- `.env` exists; runtime model is `anthropic/claude-opus-5-5`. Tests are fully offline
  (LLM stubbed in fixtures). **No paid API calls this milestone.**
- Local boot check: `export APP_ENV=development TELEGRAM_BOT_TOKEN=` and a scratch
  SQLite `DATABASE_URL`; `APP_ENV=dev` raises a webhook-secret error. No local
  `cutebot.db` in the repo; a stale one goes to `~/trash/` (`mkdir -p ~/trash && mv
  cutebot.db ~/trash/cutebot-$(date +%F-%H%M%S).db`). Never `rm -rf` anything.
- `alembic upgrade head` does not work on SQLite (migration 0002 is Postgres-only);
  verify new migrations via the test suite (`create_all`) and by reading them. Write
  backfills by hand: add nullable → `UPDATE` → `ALTER … SET NOT NULL`.
- `brand.md` is gitignored and owner-owned; in prod it lives on the Railway volume
  (`BRAND_FILE=/data/brand.md`). Never edit it; the app may only *read* it as a seed.
- Long commands (pytest, uvicorn) run in the background; don't sleep in the foreground.

### Working rules

- Branch **`m10-tenant` exists locally**: `git checkout m10-tenant` (never branch from
  `main`, never rebase, never create a worktree or another branch — `EnterWorktree` is
  off limits; this checkout is the only one). Small, focused commits; message ends with
  `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- Linear: set your ticket **In Progress** when you start and **Done** with a short
  comment (what shipped, commit hash) when it's green. Never create tickets; work you
  find outside the plan goes in the final summary.
- Follow the plan. If it is wrong against the code, pick the smallest change that keeps
  its intent, add a `# ponytail:` comment where you simplified, and say so in the
  summary. Only touch what your tickets name.
- Every non-trivial branch/loop/parser gets a test. Keep the suite offline.
- Telegram: do not send messages to the real chat; verify with the offline tests
  (`tests/test_telegram.py`, `_FakeNotifier`, `_FakeHttp`). The console card and
  slash menu must look exactly as before.

### Hard gates (stop and leave for me)

- **Do not merge to `main`** (auto-deploys to Railway and runs migrations 0009/0010 on
  the production DB). **Do not push** unless your session block says so.
- Do not touch Railway (`railway up`, variables, scaling, `connect`). Do not edit
  `brand.md` or `.env`.

### Final message

Research / Plan / Changes / Reflect sections, ticket status, deviations from the plan,
and non-obvious findings saved to `~/.claude/projects/-home-yoav-Projects-cutebot/memory/`
(or global memory per `CLAUDE.md`) — mandatory, not optional.

---

## Session 1 — CUT-74 · M10.1 brand row + brand_id seam · Opus 5.5

Your ticket is **CUT-74**: the `Brand` model and migration `0009_brands` (create the
table, insert row 1 from the env, add `brand_id` to posts / batches / feedback /
settings nullable → backfill 1 → NOT NULL, recreate `banned_images` with the
`(brand_id, image_ref)` key, `settings` key becomes `(brand_id, key)`), `app/brands.py`
(`default`, `by_chat_id`, `ensure_default` seeding `brand_text` from `BRAND_FILE` when
empty, `text`), `ensure_default` in the lifespan, and the brand threaded through
`generate` / `learning` / `stock` / `prefs` / `queue` / `review` / `publish` /
`scheduler` / `telegram`. The webhook resolves the chat to a brand; an unknown chat is
ignored as today.

Leave behind for session 2: `brands.text(brand)` is the single read of the brand text
(session 2 makes `/distill` write the row); `generate_batch` keeps the LiteLLM usage
block reachable per post (session 2 stores tokens and cost); `/status` keeps one
function that renders the detailed view (session 2 appends a cost line).

Done = ticket checklist green, gate green, fresh-SQLite boot creates brand 1 and seeds
its text, a dry-run cycle writes `brand_id = 1` everywhere, commits on `m10-tenant`,
CUT-74 Done in Linear. **Commit and stop; do not push.**

---

## Session 2 — CUT-75 + CUT-76 · M10.2 brand text in DB, M10.3 cost, close-out · Sonnet 5.5

Prerequisite: CUT-74 is Done and `git log --oneline main..m10-tenant` shows its commits.
If not, stop and say so.

Your tickets, in order:

1. **CUT-75**: `/distill` proposes from and applies to the brand row (`brand_text_prev`
   keeps the old text, no more `brand.md.bak`), `/brand` (typed only, shows the text),
   `scripts/brand_sync.py push | pull`, docs (DEV_GUIDELINES, SCRIPTS_REFERENCE,
   PRODUCT_SPEC).
2. **CUT-76**: migration `0010_cost`, `llm.cost_usd` (moved from
   `scripts/eval_models.py`, which imports it), tokens + cost stored in
   `generate_batch`, batch sum, one cost line in `/status`.
3. **Close-out (CUT-73)**: ROADMAP M10 row + "Next" header, PRODUCT_SPEC data section,
   CLAUDE.md summary line, `docs/plans/M10_PLAN.md` and this file moved to
   `docs/archive/`, SCRIPTS_REFERENCE checked against every new command and script.

Done = both tickets green in Linear with comments, gate green, fresh-SQLite boot clean,
commits on `m10-tenant`. Then **push `m10-tenant` and open a PR to `main`** with a
per-ticket summary and the deploy notes (two migrations; `brand.md` becomes the seed).
**Never merge.** Stop after the PR link.

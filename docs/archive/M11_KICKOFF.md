# M11 kickoff prompts — two sessions, split by model

The three tickets share files (`stock.py`, `telegram.py`, `models.py`, migrations), so they
run **one after another on the same branch**. Two sessions, not three: a session boundary
only pays for itself when the model changes. One laptop, one checkout: **no worktrees, no
extra branches** — every session works in `~/Projects/cutebot` on `m11-media`, one session
at a time.

| Session | Tickets | Model | Why |
|---------|---------|-------|-----|
| 1 | CUT-78 M11.1 files/albums/provenance, CUT-79 M11.2 stock browser | **Sonnet 5.5** | Telegram handlers and a pager over code and fakes that already exist; mechanical once the plan is read. |
| 2 | CUT-80 M11.3 Google Drive sync + write-back, close-out | **Opus 5.5** | New external integration with auth, idempotent sync and write-back into the owner's own files; a wrong assumption costs real data. |

For each session: a subagent (or a fresh Claude Code session in `~/Projects/cutebot` with
`/model` set as above) gets the **Common block** and then its session block.

---

## Common block (paste first, every session)

You are implementing part of milestone **M11 — Media v2** for CuteBot without checking in
with me. Work until your tickets are done or genuinely blocked, then stop with a summary.

### Read first (in this order)

1. `CLAUDE.md` and `docs/DEV_GUIDELINES.md` — project rules (approval gate is load-bearing;
   structured LLM output; migrations for DB changes; lint + types + tests before moving on).
2. `docs/archive/M11_PLAN.md` — the approved plan. Canonical for scope and design; the
   decisions under "Risks and decisions" were confirmed on 2026-10-21 and are not up for
   debate. Read all of it, not only your section.
3. Your Linear tickets (named in your session block) and the epic **CUT-77**. Linear MCP
   tools, team "CuteBot".
4. `app/stock.py`, `app/brands.py`, `app/models.py`, the photo-upload and `/stock` parts of
   `app/notifier/telegram.py`, `tests/test_telegram.py` (`_FakeNotifier`, `_FakeHttp`),
   `tests/test_brands.py` — what M10 left you. `git log --oneline main..m11-media` — what the
   earlier session already shipped.

### Environment

- Python env: `source .venv/bin/activate` (uv-managed, Python 3.12). Never the system python.
  Gates are the pinned tools: `.venv/bin/ruff check . && .venv/bin/mypy app &&
  .venv/bin/pytest` (`uvx ruff` is newer and reports noise that is not a gate).
- `.env` exists; runtime model is `anthropic/claude-opus-5-5`. Tests are fully offline (LLM
  and Telegram stubbed in fixtures). **No paid API calls, no real Telegram sends, no real
  Google calls this milestone.**
- Local boot check: `export APP_ENV=development TELEGRAM_BOT_TOKEN=` and a **fresh** scratch
  SQLite `DATABASE_URL` under `/tmp/claude-1000/.../scratchpad/boot-$(date +%s).db`
  (`APP_ENV=dev` raises a webhook-secret error). Run uvicorn as
  `timeout 15 .venv/bin/uvicorn app.main:app --port 8002` in the background and curl
  `/health`; never `kill`.
- `alembic upgrade head` does not work on SQLite (migration 0002 is Postgres-only); verify new
  migrations via the test suite (`create_all`) and by reading them. New dependency ⇒
  `requirements.txt` **and** `pyproject.toml`, then `uv pip install -r requirements.txt`.
- The stock library root is `settings.stock_images_dir` (prod: on the Railway volume); per
  brand it is `stock.brand_dir(settings, brand.stock_prefix)`. Tests use `tmp_path`.
- Long commands (pytest, uvicorn) run in the background; don't sleep in the foreground.

### Forbidden in Bash

Each of these hits a human gate and, with nobody at the keyboard, hangs you for hours:
`rm` (any form, outside `/tmp/claude-1000/`), `kill`/`pkill`, `git push`, `railway up`,
`alembic upgrade` against a real DB, `dropdb`. Instead: a fresh scratch file name rather than
deleting (`mkdir -p ~/trash && mv <path> ~/trash/<name>-$(date +%F-%H%M%S)` for real files);
`timeout 15 …` rather than kill; commit on the branch and report — the orchestrator pushes
unless your session block says otherwise. A command the guard hook refuses means change
approach; never retry it verbatim.

### Working rules

- Branch **`m11-media` exists locally**: `git checkout m11-media` (never branch from `main`,
  never rebase, never create a worktree or another branch — `EnterWorktree` is off limits;
  this checkout is the only one). Small, focused commits, one per ticket at least; message
  ends with `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- Linear: set your ticket **In Progress** when you start and **Done** with a short comment
  (what shipped, commit hash) when it's green. Never create tickets; work you find outside the
  plan goes in the final summary.
- Follow the plan. If it is wrong against the code, pick the smallest change that keeps its
  intent, add a `# ponytail:` comment where you simplified, and say so in the summary. Only
  touch what your tickets name.
- Every non-trivial branch/loop/parser gets a test. Keep the suite offline.
- Telegram: verify with the offline tests only. The console card, slash menu and every
  existing command must look exactly as before; new buttons appear only where the plan says.
- `docs/SCRIPTS_REFERENCE.md` gets every new command, callback prefix and script.

### Hard gates (stop and leave for me)

- **Do not merge to `main`** (auto-deploys to Railway and runs migration 0011 on the
  production DB). **Do not push** unless your session block says so.
- Do not touch Railway (`railway up`, variables, scaling, `connect`). Do not edit `brand.md`
  or `.env`.

### Final message

Research / Plan / Changes / Reflect sections, ticket status, deviations from the plan, and
non-obvious findings saved to `~/.claude/projects/-home-yoav-Projects-cutebot/memory/` (or
global memory per `CLAUDE.md`) — mandatory, not optional.

---

## Session 1 — CUT-78 + CUT-79 · M11.1 files, albums, provenance; M11.2 stock browser · Sonnet 5.5

Your tickets, in order:

1. **CUT-78 M11.1**: migration `0011_stock_images` (table `stock_images` with composite key
   `(brand_id, image_ref)`, `source` in `telegram|drive|manual`, `source_id` null, `sha256`,
   `imported_at`, `deleted_at` null, index `(brand_id, sha256)`; `brands.drive_folder_id` and
   `brands.drive_synced_at` nullable — session 2 uses them) and the `StockImage` model.
   `stock.register(session, brand, path, source, source_id)` is the one way a file enters the
   library: computes SHA-256, skips an existing hash for the brand (returns `None`), converts
   HEIC/HEIF to JPEG via `pillow-heif` (new dependency), writes the row. The existing
   compressed `photo` upload routes through it. New: `document` messages whose MIME is an
   image are downloaded as originals (`file_size` > 20 MB ⇒ reply "too large for Telegram —
   use Drive"). Media groups: save each file on arrival, reply once per `media_group_id`
   after a 2-second quiet period ("Saved 7 photos, 1 duplicate skipped"); keep the state
   in-process (a dict + `asyncio` timer), no table. `/help` and the low-stock message say
   "send as file for full quality". A file on the volume with no row is treated as `manual`.
2. **CUT-79 M11.2**: `/stock` keeps its text summary; its sub-menu gains **Browse**. Page N:
   one `sendMediaGroup` of up to 10 thumbnails (320 px, captions `1`..`10` with used-count and
   last-used date from `posts`) then one control message with rows `🚫 1..10`, `🗑 1..10` and
   `‹ Prev` / `Next ›` / `‹ Menu` (callback prefix `stk:`; route it in `_console_callback`).
   Ban ⇒ `stock.ban_image`; Delete ⇒ move the file to `stock/.trash/<brand>/` and set
   `deleted_at`; both toast and redraw the control message. Nightly: empty `.trash` entries
   older than 30 days (a job next to generation in `app/scheduler.py`). Home card: a warning
   line when the volume holding the stock dir is above 80 % (`shutil.disk_usage`).

Done = both tickets green in Linear with comments, gate green, fresh-SQLite boot clean,
offline tests per the plan's "Done when" (a document, a photo, an album of 3, a duplicate, a
HEIC fixture; paging over a 25-image fixture, ban, delete, empty library), commits on
`m11-media`. **Commit and stop; do not push.**

---

## Session 2 — CUT-80 · M11.3 Google Drive sync and write-back, close-out · Opus 5.5

Prerequisite: CUT-78 and CUT-79 are Done and `git log --oneline main..m11-media` shows their
commits. If not, stop and say so.

Your tickets, in order:

1. **CUT-80 M11.3**: `app/drive.py` over `httpx` + `google-auth` (new dependency): service-
   account JWT from `GOOGLE_SERVICE_ACCOUNT_JSON` (`settings.google_service_account_json`,
   blank ⇒ Drive disabled, `/drive` says so), `list_images(folder_id)` (files only, image
   MIME, not sub-folders), `download(file_id, dest)`, `move(file_id, from_parent, to_parent)`
   (an `update` on `parents`), `ensure_subfolder(folder_id, name)`. `/drive` prints the
   service-account address; `/drive <folder link or id>` verifies it can list the folder and
   stores `brands.drive_folder_id` (a folder it cannot see fails with a clear message).
   `sync(session, brand)`: download files with no `stock_images` row for their Drive id or
   sha256, `stock.register(..., source="drive", source_id=<file id>)`, set
   `drive_synced_at`, return the count. Runs nightly after generation per brand and on
   `/sync` (typed, and a button in the Stock sub-menu). Write-back, on by default: on publish
   move the post's Drive file to `posted/`, on ban to `banned/` (sub-folders created on first
   use; a file without a Drive `source_id` is skipped silently). Drive mocked in tests with a
   fake transport (3 files sync 3, a re-run syncs 0, publish ⇒ `posted/`, ban ⇒ `banned/`).
   `GOOGLE_SERVICE_ACCOUNT_JSON` in `env.example`.
2. **Close-out (CUT-77)**: ROADMAP M11 row (shipped table) + remove it from "Next",
   PRODUCT_SPEC §3 stock + data model, CLAUDE.md summary line, SCRIPTS_REFERENCE checked
   against every new command, callback prefix and job, `docs/plans/M11_PLAN.md` and this file
   moved to `docs/archive/` (fix their internal paths).

Done = ticket green in Linear with a comment, gate green, fresh-SQLite boot clean, commits on
`m11-media`. Then **push `m11-media` and open a PR to `main`** with a per-ticket summary and
the deploy notes (migration 0011; new env var `GOOGLE_SERVICE_ACCOUNT_JSON`; two new
dependencies). **Never merge.** Stop after the PR link.

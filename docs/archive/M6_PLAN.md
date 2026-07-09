# M6 — Operator Console & Feedback Signal

> Execution plan for the next milestone, per the decisions in `docs/POST_V1_REVIEW.md`
> (§5, §6, "Decisions" section). Execute in a fresh conversation; archive to
> `docs/archive/` when shipped. Scope was decided 2026-07-08: **full command set**,
> reject reasons, timezone-aware slots, misfire catch-up, DM send-failure logging.
>
> **Linear:** CUT-42 (M6.1) … CUT-48 (M6.7), one per task below. Model eval is
> CUT-49 (standalone, blocked on Anthropic credits).

## Goal

Make the Telegram bot a full operator console (nothing requires curl/DB access in
prod), and make every rejection carry a usable training signal — the data M8's
learning loop needs.

## Guardrails (unchanged, load-bearing)

- **Approval gate**: `/postnow` (with or without id) may only ever publish `APPROVED`
  posts. Anything else answers "can't — post is <status>", never mutates.
- All commands are **owner-gated** by the existing chat-id check in
  `process_message` — no new auth surface.
- DB shape changes ⇒ Alembic migration (0003). New deps: none expected.
- Suite stays offline; Telegram interactions tested via the existing
  request-capture pattern in `tests/test_telegram.py`.

## Tasks

### M6.1 — Reject reasons (the feedback-signal upgrade)

The one schema change of the milestone. Decided chips: **voice / Hebrew / image /
boring** + skip.

- `Feedback.reason: str | None` (nullable, default None) + Alembic migration 0003.
- Telegram flow: tapping ❌ applies the rejection immediately (existing
  `handle_decision`), then the message keyboard swaps to one row of reason chips —
  callback data `reason:<post_id>:<voice|hebrew|image|boring|skip>`.
- Chip tap → update the **most recent reject Feedback row** for that post with the
  reason (skip → leave None), then restore the "↩︎ Approve" flip button (M5.4
  behavior preserved).
- A reversal (approve after reject) leaves the old reasoned row intact — history is
  the training signal, never rewritten.
- Tests: reason persisted; skip leaves None; chips never appear on approve;
  PUBLISHED posts unaffected.

### M6.2 — Command dispatch + `/generate [N]` + `/postnow [id]`

- Refactor `process_message` (app/notifier/telegram.py) into a small command
  dispatch (dict of handlers) — `/status` moves in unchanged.
- `/generate [N]`: run `generate_batch(session, n=N)` (default `BATCH_SIZE`, cap N at
  e.g. 10) + `send_for_review`. Reply first with "Generating N suggestions…" since
  vision captioning takes ~5s/image.
- `/postnow [id]`: no id → existing `publish_next`. With id → new
  `publish.publish_post(session, post)` (refactor the body of `publish_next` so both
  paths share the claim→publish→finalize flow; `publish_next` becomes peek + call).
  Guard: only `APPROVED`; reply with resulting status.
- Tests: N parsing (bad input → usage hint), publish-specific-post happy path, the
  gate (suggested/rejected/published ids all refused), shared-flow refactor keeps
  `publish_next` behavior (existing tests must pass untouched).

### M6.3 — Visibility & recovery: `/queue`, `/pending`, `/requeue <id>`

- `/queue`: approved posts in queue order — `#id · queue_position · caption_he first
  ~40 chars`. Empty → "Queue is empty".
- `/pending`: undecided (`SUGGESTED`) posts, oldest first; each is **re-DM'd** via
  `send_suggestion` (covers the lost-DM gap — review doc §3.2.2). Cap at e.g. 10 per
  invocation.
- `/requeue <id>`: same guard as the dev route (`FAILED` only, via `queue.requeue`);
  closes the "FAILED post unrecoverable in prod" hole (§3.2.1).
- Tests: ordering, caps, guards.

### M6.4 — Photo upload → stock library

- In `process_message`: a message with `photo` from the owner chat → download the
  largest size via Telegram `getFile`/file download API → save into
  `STOCK_IMAGES_DIR` as `tg_<date>_<file_unique_id>.jpg` → reply "Added to stock
  (N images total)".
- Non-owner photos ignored (existing gating). Download failures reply with the error.
- Tests: saved file lands in a tmp stock dir with expected name; non-owner ignored.

### M6.5 — Timezone-aware posting slots

- New setting `SCHEDULE_TZ` (default `Asia/Jerusalem`) in config + `env.example` +
  SCRIPTS_REFERENCE (replace the "times are UTC" notes).
- `build_scheduler`: pass `settings.schedule_tz` as the `CronTrigger` timezone for
  **both** generation cron and posting slots (APScheduler handles DST natively).
- Prod note: Railway needs no var change to keep current behavior only if
  `SCHEDULE_TZ=UTC` is set there before deploy — otherwise slot times shift to
  Israel time on the next deploy. Call this out in the deploy step.
- Tests: trigger timezone matches setting.

### M6.6 — Reliability: slot catch-up + DM send logging

- **Slot catch-up** (deploy-straddles-a-slot gap, §3.2.3): APScheduler's in-memory
  store can't know about missed runs across restarts, so do it at startup: in
  `lifespan`, after `recover_orphaned` — if the queue is non-empty and a posting slot
  occurred within the last `CATCHUP_WINDOW_MIN` (default 60) and no post was
  published since that slot, run `publish_next` once. `ponytail:` startup check, not
  a persistent jobstore — revisit only if slots become per-post schedules.
- **DM send logging** (§3.2.2 root cause): `send_suggestion` checks each Telegram API
  response; non-ok → `logger.error` with post id + description (no retry loop —
  `/pending` is the recovery path).
- Tests: catch-up publishes exactly once and only when conditions hold; send failure
  logs and doesn't raise.

### M6.7 — E2E verification + docs + ship

- Manual dry run against the test chat: generate via `/generate 2` → reject one with
  a reason → approve one → `/postnow` → `/queue`/`/pending`/`/status` sane →
  photo upload lands in stock.
- Update `SCRIPTS_REFERENCE.md` (command list), `README.md` (features), `ROADMAP.md`
  (mark M6 shipped), archive this plan.
- Deploy to Railway (set `SCHEDULE_TZ` first — see M6.5), verify via prod `/status`.

## DoD

Every pipeline operation is reachable from Telegram (generate, decide with reasons,
inspect queue/pending, publish now — front or specific, requeue failed, grow the
stock); rejections carry reasons; slots run in Israel time and survive
deploy-straddling; `ruff && mypy && pytest` green; verified against the real bot and
in prod.

## Out of scope (already decided elsewhere)

Model eval (standalone task, `scripts/eval_models.py`, blocked on Anthropic credits);
Instagram publisher (M7 — but **start Meta business-verification paperwork during
M6**); learning loop & brand distillation (M8); approve-with-edits (later).

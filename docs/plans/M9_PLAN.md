# M9 — Bot console v2

> **Approved 2026-10-15.** Linear: epic **CUT-68** · M9.1 **CUT-69** (verdicts) · M9.2 **CUT-70**
> (mode + schedule) · M9.3 **CUT-71** (console UX); the M9.4 close-out checklist lives on the
> epic. Scope decided in `docs/issues/next_steps_13_10_2026.md` (items 1 + 2). This file is
> canonical for scope; the tickets carry the deliverable checklists.

## Context

The owner's stated blocker: *"I'm waiting for the learning loop to make the suggestions
better before I allow it to actually post."* Today that is impossible to express — ✅ is
both "good post" and "publish it" (`review.handle_decision` → `queue.enqueue`), so
training the model means either publishing or rejecting. The schedule is env-only
(`GENERATION_CRON`, `POSTING_SLOTS` → redeploy), and the bot speaks plain text with no
persistent buttons. M9 fixes the three things that make daily use cheap: **a verdict
vocabulary that separates "good" from "publish"** (Keep, Almost, Retry), **schedule and
mode from the chat**, and **a console that needs no typing**.

Everything stays single-tenant; M10 (tenant seam) adds `brand_id` to the new `settings`
table afterwards — a one-column migration, deliberately deferred so M9 ships fast.

**Locked decisions (2026-10-14):**
- **`approved` keeps meaning "will publish".** The approval gate is untouched; new
  verdicts get new statuses or decisions, never a flag on `approved`.
- **`kept` is a terminal status** (approved for learning only). It is never queued and
  never published; `/postnow <id>` on a kept post refuses. A kept post can be promoted to
  `approved` later via its card (`📤 Publish after all`) — that's a normal approve
  transition through `handle_decision`, so the gate still has one door.
- **`Almost` is a Decision, not a status.** The post ends `rejected`; the row is
  `Feedback(decision=almost, reason=<chip|None>)`. Learning treats it as "liked direction,
  flawed execution" — rendered separately from rejects, below approvals.
- **Retry reuses the image.** A retried post is a *new* `Post` row with the same
  `image_ref`, linked by `Post.retry_of`; the image is not returned to the pool between
  the Almost and the retry (stock selection already excludes images on live posts —
  `rejected` posts are excluded only while a retry is pending).
- **Settings live in the DB, env is the default.** `settings` table (key/value text);
  `Settings` reads env; a tiny `app/prefs.py` reads the row and falls back to env. The
  scheduler is rescheduled in place — no restart.
- **No Mini App.** Slash menu + inline keyboards + HTML. The Mini App question is
  re-asked in M11 (media gallery), not here.

## Approach

### M9.1 — Verdicts: `📚 Keep`, `🤏 Almost` (+ chips), `🔁 Retry` — `app/models.py`, `app/pipeline/review.py`, `app/learning.py`, `app/notifier/telegram.py`, migration `0007`

Data:
- `PostStatus.KEPT = "kept"`; `Decision.ALMOST = "almost"`; `Post.retry_of: int | None`
  (FK `posts.id`, nullable). Migration `0007_verdicts`: the column only — statuses and
  decisions are strings already (`String(16)`), no enum DDL.
- `Feedback.decision` gains the value `almost`; `reason` reuses the existing chips
  (`voice` / `hebrew` / `image` / `boring`) or `None` (skip). `ban_image` is not offered
  on Almost (the image was fine by definition).

Review (`handle_decision`):
- `decision="keep"` → `Feedback(decision=approve)` **plus** `post.status = KEPT`,
  `queue_position = None`. (Keep *is* an approval signal; the status is what blocks
  publishing. ponytail: no third Decision value for Keep — the learning query wants
  "owner liked it", and `status == kept` already says "didn't post".)
- `decision="almost"` → `Feedback(decision=almost, reason)`, `post.status = REJECTED`.
- Reversals: `kept ↔ approved` allowed (same rules as `approved ↔ rejected` today);
  `kept → rejected` allowed; terminal statuses untouched. Idempotent repeats are no-ops.
- `retry(session, post_id, settings) -> Post`: loads the almost'd post, calls
  `llm.caption_image` with the **same image** and a `retry_of` hint in the learning
  context (`<retry_of>` block: the previous caption + the chip, "the owner liked this
  direction; it wasn't good enough — try again, different execution"), inserts a new
  `Post(status=suggested, retry_of=post.id, batch_id=post.batch_id)` and sends it for
  review. One retry per Almost (a second 🤏 on the retry is allowed, chain is fine).

Learning (`build_context` / `render`):
- `_LIVE` gains `KEPT` (positive examples).
- New `near_misses: list[NearMiss]` (`caption_he`, `reason | None`), last
  `LEARNING_EXAMPLES` Almost rows in the window → `<near_misses>` block: "The owner liked
  the direction of these but did not post them; learn the direction, fix the flaw."
- `summary()` adds `· N near-misses`; `/status` shows `learning: 12 ✅ · 7 🤏 · 20 ❌`
  (decision counts in the window) — the number that tells the owner when to go live.
- Retry pairs: when a retried post is approved/kept, the (almost caption → approved
  caption) pair is rendered in `<owner_corrections>` exactly like a ✏️ Fix edit — no new
  storage, it's a join on `retry_of`.

Telegram:
- Card keyboard becomes two rows: `✅ Approve · 📚 Keep · ✏️ Fix` / `🤏 Almost · ❌ Reject`.
- `almost:<id>` → chip picker (same `show_reason_picker`, title "What's off?", no
  ban chip, plus `skip`) → `almostr:<id>:<chip|skip>` applies the verdict and edits the
  card to `🤏 Almost (hebrew) · 🔁 Retry` with a `retry:<id>` button.
- `keep:<id>` → `📚 Kept (learning only)` with `📤 Publish after all` (`approve:<id>`) and
  `↩︎ Reject`. Approved cards gain `📚 Keep instead`.
- `retry:<id>` → toast "🔁 Generating a retry…", new card arrives; the old card's Retry
  button is removed (`retried → #<new id>`).
- `/postnow <id>` on a kept post: "Post #id is kept (learning only) — tap 📤 on its card".

Tests: status transitions (keep/almost/retry, reversals, terminal untouched, kept never
in `peek_next`), learning blocks (near-miss rendering, retry pair shows as correction,
kept counts as example), callback routing for the four new prefixes, stock selection
holds the image while a retry is pending.

### M9.2 — `/mode learning|live` + `/schedule` + runtime reschedule — `app/prefs.py` (new), `app/scheduler.py`, `app/notifier/telegram.py`, migration `0008`

Data: `settings` table (`key TEXT PK, value TEXT, updated_at`). Keys in M9:
`mode` (`live` | `learning`), `generation_cron`, `posting_slots`. `app/prefs.py`:
`get(session, key, default)`, `set(session, key, value)`, plus typed helpers
`mode()`, `generation_cron()`, `posting_slots()` that fall back to `Settings`.

Mode:
- `learning`: every ✅ on a card behaves as `📚 Keep` (the toast says so: "📚 Kept —
  learning mode"); posting-slot jobs are **paused** (`scheduler.pause_job`), not removed;
  `/postnow` refuses ("learning mode — /mode live to publish"); the `/status` header
  shows `🎓 LEARNING MODE — nothing publishes`. Generation keeps running.
- `live`: default; restores slot jobs (`resume_job`). Switching modes DMs a one-line
  confirmation with what changed.
- Startup reads the mode and applies it before `scheduler.start()`.

Schedule:
- `/schedule` → current generation cron + slots, in `SCHEDULE_TZ`, plus next-run times
  from the scheduler (the honest view: what *will* fire).
- `/schedule post 12:00,18:00` → validates `HH:MM` list (1–6 slots), stores, then
  removes `posting-*` jobs and re-adds them (`build_scheduler` gets a
  `apply_slots(scheduler, slots)` helper used by both boot and this command).
- `/schedule generate 09:00` (daily at) or `/schedule generate "0 9 * * 1-5"` (raw cron,
  validated by `CronTrigger.from_crontab`) → stores and `reschedule_job("generation")`.
- `/schedule reset` → delete the rows, back to env defaults.
- Catch-up (`publish.catch_up_missed_slot`) and the generation tick read slots via
  `prefs`, not `Settings`, so the DB value is the only truth at runtime.

Tests: prefs fallback (no row → env value), slot validation (bad input → usage message,
nothing stored), reschedule replaces job ids, learning mode turns approve into keep and
pauses slot jobs, `/postnow` refusal, catch-up uses stored slots.

### M9.3 — Console UX: slash menu, HTML output, drill-down buttons — `app/notifier/telegram.py`

- **Slash menu** (the native "/" list, `setMyCommands`): one table drives the menu and
  `/help` — `_COMMANDS: list[tuple[group, command, menu_label, usage]]`. Menu labels are
  plain text (no emoji), verb-first, ≤ 32 chars so they don't truncate on a phone;
  argument hints move out of the menu into `/help`. Order = frequency, settings last:
  `status Pipeline, queue and stock counts` · `queue Approved posts, in order` ·
  `pending Resend cards awaiting review` · `generate Draft a batch now` ·
  `postnow Publish the next queued post` · `stock Stock library` ·
  `schedule View or change the slots` · `mode Learning / live` ·
  `distill Propose a brand.md revision` · `help All commands + arguments`.
  `requeue` leaves the menu (rare; still a typed command, listed in `/help`). The list
  is registered with `scope: {type: chat, chat_id: owner}` so only the owner's chat
  shows it; the default scope stays empty.
- **No reply keyboard.** It would duplicate the slash menu and permanently eat the
  keyboard area; the one-tap surfaces live on the messages instead (below).
- **HTML** (`parse_mode=HTML`, escaped values): `/status` as sections with bold headers
  and a `<code>` counts block; `/queue` rows as `#id · pos · preview` with inline
  `▶ Post now` / `❌ Drop` buttons per row (callbacks `postnow:<id>` → existing
  `publish_by_id`; `drop:<id>` → `handle_decision(reject)`); `/stock` banned list as
  rows with `♻️ Unban` buttons (`unban:<ref>` — ref length ≤ 64 bytes of callback data,
  else fall back to the text command); `/pending` resends with a count line. `/mode`
  answers with a confirmation inline keyboard (`mode:learning` / `mode:live` / cancel) —
  mode is the one thing that must not flip by accident.
- `/help` grouped: *Review a card* · *Pipeline* · *Library* · *Settings*, each 2–4 lines,
  generated from the same table.
- `send_message(text, *, html=False, keyboard=None)` grows the two keyword args; every
  existing call stays valid.
- Tests: menu payload shape (labels ≤ 32 chars, no emoji, owner scope), `/help` lists every
  command incl. `requeue`, HTML escaping of captions in `/queue`, the new callbacks
  route to the existing functions.

## Sequencing

M9.1 → M9.2 → M9.3 → M9.4. M9.1 and M9.2 touch `handle_decision` and the card keyboards
from different sides, so they are sequential; M9.3 is pure presentation on top of both.
One branch `m9-console`, one PR, migrations `0007` + `0008` run on deploy.

## Risks

- **Learning mode must fail closed.** If the `settings` row is unreadable at boot, the
  scheduler starts in *learning* mode (slots paused) and DMs a warning — never the other
  way round.
- **Callback-data size** — Telegram caps `callback_data` at 64 bytes; `unban:<ref>` uses
  the filename only, and long names fall back to the text command.
- **Card edit races** — a Retry while the original card is being edited; the Retry
  button is removed by editing the old card *after* the new post is sent, so a lost edit
  only leaves a stale button that answers "already retried → #id".
- **Menu scope** — a chat-scoped `setMyCommands` only shows in the owner chat; if the
  call is rejected (e.g. chat id not yet known at boot) fall back to the default scope so
  the menu never disappears.

## Definition of done

- `ruff check . && mypy app && pytest` green; fresh-SQLite boot registers the jobs and
  applies the stored mode.
- In a test chat: Keep / Almost (+chip) / Retry / Fix / Approve / Reject round-trips; a
  kept post never appears in `/queue` or publishes; `/mode learning` pauses slots and
  turns ✅ into Keep; `/schedule post 10:00` changes the next-run time without restart;
  the slash menu shows the new labels after a restart.
- `/status` shows the verdict counts and the mode; docs updated; plan archived.

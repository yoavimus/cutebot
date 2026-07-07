# M3 — End-to-end dry run (publish path, verified & crash-safe)

> Implementation plan for milestone **M3** in `ROADMAP.md`. To be executed in a later
> session. `PRODUCT_SPEC.md` is the source of truth. Predecessors: M0, M1, M2 (shipped —
> plans in `docs/archive/`).

## Context

The publish path is already wired end to end: `scheduler.build_scheduler` registers one
`posting_tick` job per slot (`scheduler.py:41`), each calls `publish.publish_next`
(`publish.py:22`), which drains the front of the queue via `queue.peek_next`
(`queue.py:27`, lowest `queue_position` among `APPROVED`) and broadcasts to every stub
publisher carrying the image (M0). M3 is mostly **proving** this loop and closing the one
real hole in it — not building new machinery.

**The one real bug (the meat of M3):** `publish_next` sets `PUBLISHING` and commits
*before* any network call (`publish.py:33-34`) — correct, it prevents a double-post on
retry. But `peek_next` only ever selects `APPROVED` (`queue.py:31`). So if the process
crashes (or the slot job is killed) **after** the `PUBLISHING` commit and **before** the
final `PUBLISHED`/`FAILED` commit, the post is stranded in `PUBLISHING` forever: never
re-picked, never completed. The docstring's "a crash/retry can't double-post" is true, but
incomplete — it trades a double-post for an orphan. M3 makes the crash *recoverable*.

**Grounding in the code:**
- `publish_next` (`publish.py:22`): peek → claim `PUBLISHING` (commit) → publish to all →
  all-ok ⇒ `PUBLISHED` + `published_at` + `queue_position=None`; any failure ⇒ `FAILED`
  (commit). Per-network exceptions are already caught and recorded (`publish.py:41-45`).
- `FAILED` keeps its `queue_position` (only the success path nulls it, `publish.py:50`),
  but `peek_next` filters on `APPROVED` — so a `FAILED` post silently drops out of the
  queue. "Re-queueable later" (ROADMAP M1) isn't actually wired.
- `PostStatus` (`models.py:18`) already has all six states; no new column needed.
- Dev routes exist: `POST /dev/publish-next` (`main.py:81`) drains one, `POST
  /dev/generate` (`main.py:71`). Good enough to drive a manual dry run.

**Locked decisions:**
- **No new DB column, no migration.** Recovery keys off the existing `PUBLISHING` status.
- **Crash recovery = a startup sweep, not a timeout/heartbeat.** In-process single-worker
  v1: on boot, any post still in `PUBLISHING` is by definition orphaned (no other worker
  could be mid-publish). Reset it and let the next slot re-pick it. No per-post lease, no
  reaper thread. (`ponytail:` startup sweep; add a lease only if v1 ever goes multi-worker
  — that's M4+/multi-tenant territory.)
- **`FAILED` stays out of the auto-queue.** A failed publish is not silently retried into
  the feed; requeue is a deliberate manual action (a dev route), not automatic. Revisit if
  real publishers make transient failures common.

## Approach

### 1. Recover orphaned `PUBLISHING` posts on startup — `app/pipeline/publish.py`
Add `async def recover_orphaned(session) -> int`: find all posts in `PUBLISHING`, reset
each to `APPROVED` (its `queue_position` is still set, so it lands back in place at the
front), commit, return the count. Because the claim happens before any send, a re-pick
re-publishes from scratch — the stub publishers are the only side effect and they're
idempotent by nature (log lines). Real-publisher idempotency (dedup keys) is explicitly a
post-v1 concern (see Out of scope).

Call it once from the app lifespan startup in `app/main.py` (where the scheduler is built
/ started), logging `Recovered N orphaned publishing post(s).` No new module.

### 2. Requeue a failed post — `app/pipeline/queue.py` + a dev route
Add `async def requeue(session, post) -> None`: if `post.status == FAILED`, move it back to
the **back** of the queue (reuse the `max_pos + 1` logic already in `enqueue`) and set
`APPROVED`. Expose `POST /dev/requeue/{post_id}` in `main.py` for the dry run / manual
recovery. Small, explicit, no auto-retry loop.

### 3. Confirm ordering + single-drain — no code, covered by tests (§5)
`peek_next` already orders by `queue_position ASC LIMIT 1` and the success path nulls the
position, so each `publish_next` drains exactly the front post and advances. This is a
*verification* item, not new code — the test in §5 is the deliverable.

### 4. Concurrency note (no code this milestone)
`peek_next` (SELECT) → claim `PUBLISHING` (UPDATE) is not atomic. In v1 it doesn't matter:
one in-process `AsyncIOScheduler`, `max_instances=1` per job, slots minutes apart, SQLite
single-writer — two ticks can't realistically interleave on the same post. Leave a
`ponytail:` comment at the peek→claim seam naming the ceiling and the fix
(`SELECT … FOR UPDATE SKIP LOCKED` on Postgres) so M4 picks it up. Do **not** build locking
now.

### 5. Tests — `tests/` (offline)
Drive the whole path with the existing offline stubs (stub publishers, monkeypatched
`caption_image`/`select_images`), no network:
- **Ordering / single-drain:** enqueue three approved posts; `publish_next` three times;
  assert they publish in `queue_position` order, each exactly once, and the queue empties.
- **Idempotency / no double-post:** a post already in `PUBLISHING` is not re-peeked by a
  concurrent-style second `publish_next` call (returns the *next* post or `None`, never
  re-publishes the claimed one).
- **Crash recovery:** manually leave a post in `PUBLISHING` (simulate the crash between
  commits); `recover_orphaned` resets it to `APPROVED` with its `queue_position` intact;
  the next `publish_next` publishes it once → `PUBLISHED`.
- **Failure path + requeue:** a publisher stub that returns `ok=False` (or raises) ⇒ post
  `FAILED`, dropped from `peek_next`; `queue.requeue` puts it back and it publishes cleanly.
- **State transitions:** assert the full `SUGGESTED → APPROVED → PUBLISHING → PUBLISHED`
  arc and `published_at` set on success only.

## Files
- Modify: `app/pipeline/publish.py` (add `recover_orphaned`; `ponytail:` note at the
  peek→claim seam), `app/pipeline/queue.py` (add `requeue`), `app/main.py` (call
  `recover_orphaned` on startup; add `/dev/requeue/{post_id}`), `tests/test_pipeline.py`,
  `SCRIPTS_REFERENCE.md` if any run steps change.
- Add: none required.
- **No DB shape change ⇒ no migration.**

## Verification
1. `ruff check . && mypy app && pytest` — green, offline.
2. **Real slot dry run** (SQLite, app on `127.0.0.1:8002`): generate a batch, approve 2–3
   via Telegram so they enqueue, set a posting slot a minute out (or hit
   `POST /dev/publish-next`): posts publish **in queue order, once each**; logs show each
   stub network receiving the bilingual caption + disclaimer + `image_ref`; state ends
   `PUBLISHED` with `published_at`.
3. **Crash recovery:** kill the app with a post left in `PUBLISHING` (or seed one), restart:
   startup logs `Recovered 1 orphaned publishing post(s).`, and the next slot publishes it
   exactly once — no double-post.
4. **Failure + requeue:** force a stub failure ⇒ post `FAILED`, absent from the queue;
   `POST /dev/requeue/{id}` returns it to the back; it then publishes clean.

## Out of scope (later milestones)
Postgres + `alembic upgrade head` on Railway and `FOR UPDATE SKIP LOCKED` (M4); real
network publishers and their **publish-dedup keys** for true at-most-once delivery
(post-v1, M-E); multi-worker leases / heartbeat reaping (only if v1 ever leaves
single-process); automatic retry-with-backoff of `FAILED` posts (manual requeue is enough
for the dry run). No new scheduler features — slots and generation cron are unchanged.

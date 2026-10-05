# current issues after m4 shipped:

## user input - 

1. app always loads the same 5 images, i want it to pick images randomly and i want images of approved posts to not repeat.

2. if not too difficult i want to be able to change approved to rejected and vise versa

3. the brand yaml file is too complex and unnneccesarily so, i want to be able to easilty edit this and not get into much formatting issues as i had, and i also want non tech costumers to be able to do it easily. also in the future there will be image generation and maybe multi-image posts, which the current format doesnt handle

4. i want to be able to easilty set the times of post suggestions and the times of posting, preferably through the telegram bot, but its not critical i can so whatever just make it simple

5. its weird that to check if post were approved and published, and that you needed so much sleep time. this process needs to be much faster and easier. maybe adding logs could help? maybe not

## model response -

> Research only — no code changed. Each item: root cause (with `file:line`), what a fix
> takes, rough effort, and a recommendation. Grouped at the end into a suggested **M5 —
> post-v1 usability** milestone.

### Quick map

| # | Issue | Effort | Root cause |
|---|-------|--------|-----------|
| 3 | brand.yaml too complex | **trivial** | file is injected as raw text — YAML does nothing |
| 5 | slow to check status | **small, high-value** | no status view + thin dev responses → m4 session hand-rolled 17 SQL queries + dozens of sleeps |
| 1 | same 5 images / no random | **small** | deterministic `sorted()[:n]`, cycles when pool exhausted |
| 2 | flip approve↔reject | **medium** | decision locked once ≠ SUGGESTED; needs pre-publish guard + UI |
| 4 | set times via Telegram | **large** (env: trivial) | schedule is env-only; no Telegram command handling |

---

### 1. Random image pick + approved images never repeat

**Root cause.** `select_images` (`app/stock.py:33-54`) builds `unused` from a
`sorted()` list (`stock.py:30`) and takes `unused[:n]` — **deterministic, never random**.
It dedups `used_refs` against **every** `Post.image_ref` regardless of status
(`stock.py:45`). When the unused pool runs dry it cycles `used[i % len(used)]` in sorted
order (`stock.py:53`) — so with a small library (≈5 images) every batch after the first
returns the *same images in the same order*. That's the "always the same 5."

**What a fix takes (small):**
- Randomize the pick: `random.sample(unused, min(n, len(unused)))` instead of `unused[:n]`
  (stdlib `random`, one import).
- Redefine "used" for the *hard* no-repeat rule: dedup only against images tied to posts
  in `APPROVED`/`PUBLISHING`/`PUBLISHED` (the committed ones), so a **rejected** suggestion
  frees its image to be tried again. The user asked specifically for *approved* posts not
  to repeat — current code is both too broad (blocks rejected too) and too weak (cycles
  into repeats anyway).
- Decide the exhaustion behaviour: if every image is already approved/published, either
  return fewer than `n` (and log a clear "stock library exhausted — add more images") or
  knowingly allow reuse of non-approved images. A hard "approved never repeats" guarantee
  is only possible while the library is larger than the total approved set — worth stating
  to the user: **the stock library has to keep growing, or the pool eventually runs out.**

**Recommendation.** Do it — small change to one function. Pair it with the exhaustion
warning so the constraint is honest. `ponytail:` in-Python random sample, no need for
`ORDER BY RANDOM()` at this scale.

### 2. Change approved → rejected and vice-versa

**Root cause.** `handle_decision` (`app/pipeline/review.py:38-40`) returns early whenever
`post.status != SUGGESTED`, so a decision is final. This idempotency guard is what makes
double-taps safe (M2) — reversing has to be added deliberately, not by loosening the guard.

**What a fix takes (medium):**
- Allow the transitions **APPROVED↔REJECTED only while the post hasn't left the queue**
  (i.e. not `PUBLISHING`/`PUBLISHED`). APPROVE→REJECT: clear `queue_position`, set
  `REJECTED`; REJECT→APPROVE: `queue.enqueue` again. Each writes a new `Feedback` row (the
  reversal is itself a training signal — good).
- **Hard guard:** never flip a `PUBLISHING`/`PUBLISHED` post. The approval gate is
  load-bearing (DEV_GUIDELINES) — you can't un-publish. This is the one real risk to get
  right.
- UI: M2 removes the buttons and edits the message after a decision
  (`telegram.mark_decided`). To re-decide you need a way back — simplest is to leave the
  **opposite** button on the message (after Approve, show a "↩︎ Reject" button, and vice
  versa) until the post publishes, then remove it. Touches `mark_decided`,
  `process_callback`, and `handle_decision`.

**Recommendation.** Worth doing; scope it explicitly to pre-publish posts and keep the
published-state guard airtight. Medium effort, mostly in the Telegram layer.

### 3. Simpler brand file (non-tech editable; future image-gen / multi-image)

**Finding (this is the easy one).** `load_brand` (`app/brand.py:20-29`) does
`brand_path.read_text()` and the text is injected **verbatim** into the prompt
(`brand.py:1` docstring; used in `llm.caption_image`). **Nothing parses the YAML** — the
structure, indentation, and keys are meaningless to the code; the model just reads it as
text. So every formatting headache the user hit was self-imposed by using YAML for
something that was never parsed.

**What a fix takes (trivial):**
- Switch the brand file to **Markdown or plain text** (`brand.md`): headings + bullet
  prose a non-technical owner can edit with zero indentation/quoting traps. Update the
  `brand_file` default (`config.py:44`) and the `_DEFAULT_BRAND` sample (`brand.py:9-17`).
  No parser to write — `read_text` already handles anything.
- Future image-gen / multi-image: because the file is free text, it already "supports"
  them — you just add a section describing image direction. No schema to migrate. (If a
  *structured* brand ever becomes necessary, that's a separate, later call — YAGNI now.)

**Recommendation.** Do it now — near-zero effort, removes the whole class of formatting
pain, and is strictly better for non-tech customers. Keep the file free-form Markdown.

### 4. Set suggestion/posting times easily — ideally via Telegram

**Root cause.** `generation_cron` and `posting_slots` are env/config
(`config.py:42-43`), read **once** when `build_scheduler` runs (`scheduler.py:35-47`).
Changing them today means editing env vars and restarting. The Telegram webhook only
handles `callback_query` updates (`main.py:62-64`) — there is **no message/command
handling**, so `/schedule`-style commands are net-new surface.

**Options, cheapest first:**
- **Env vars (trivial, already works):** document `GENERATION_CRON` and `POSTING_SLOTS` in
  `env.example` + README; on Railway they're just variables (restart to apply). Meets
  "just make it simple."
- **A dev endpoint (small):** `POST /dev/schedule` that updates the values and calls
  `scheduler.reschedule_job(...)` live — no restart, no Telegram.
- **Telegram command (large):** add message-handling to the webhook/poller, parse
  `/schedule gen 9:00` / `/schedule post 12:00,18:00`, **persist** it (a settings row or a
  small JSON file — env can't be written at runtime), and reschedule APScheduler jobs. This
  is the biggest of the five: new update type + persistence + live reschedule.

**Recommendation.** The user said it's not critical and "whatever is simple" — so ship the
**env-var** path now (document it), and only build the Telegram command later if editing a
variable proves too clumsy. If some in-app control is wanted without the Telegram cost, the
`/dev/schedule` endpoint is the middle ground.

**DECIDED (user):** env-var change + redeploy is acceptable. No Telegram command, no
`/dev/schedule` endpoint. Scope reduces to **documentation only** — make
`GENERATION_CRON` / `POSTING_SLOTS` clearly editable in `env.example` + README. Ticket
= CUT (M5.5).

### 5. Faster/easier way to see if posts were approved & published

**Evidence (the "m4 implementer sonnet" session).** This is the user's actual point, and
the transcript proves it isn't a "you didn't know the dev triggers" problem — the triggers
were used *heavily* and were **still** not enough. In that one session the implementer:
- hit `POST /dev/generate` and `POST /dev/publish-next` **~50 times each** and
  `/dev/requeue` ~39 times — a tedious poke-one-stage-at-a-time loop;
- ran **17 raw `sqlite3` `SELECT … FROM posts`** queries to *see* state, hand-rolling the
  exact view that's missing — e.g. `SELECT id, status, queue_position FROM posts ORDER BY
  id DESC LIMIT 10` and caption dumps `SELECT id, image_ref, caption_he, caption_en FROM
  posts WHERE id IN (…)`;
- scattered **dozens of `sleep 3/4/5/30`** — mostly waiting for the app to boot (polling
  `/health`) and for the LLM behind `/dev/generate` to finish before anything could be
  inspected.

So the friction has three distinct causes, only one of which I named originally:

**Root causes.**
1. **No status view.** State is invisible; the only way to see it is raw SQL. The 17
   hand-rolled `SELECT … FROM posts` are the missing endpoint, drawn by hand 17 times.
2. **Dev triggers return too little.** `POST /dev/generate` returns just
   `{"generated": [ids]}` (`main.py:81`) and `/dev/publish-next` just the published id
   (`main.py:88`) — so *every* call is followed by a DB query to see the captions,
   `image_ref`, or resulting status. The response should carry the finished objects.
3. **Blind sleeping.** No readiness signal on boot and no synchronous "it's done" — so time
   is burned on fixed `sleep`s instead of observing a result.

**What a fix takes (small, high-leverage):**
- Add a read-only **status view** `GET /dev/status` → each post's `id`, `status`,
  `queue_position`, `decided_at`, `published_at`, `batch_id` (+ truncated captions). This
  alone replaces all 17 manual queries and most of the `sqlite3` reaching.
- Make the dev triggers **return the objects they just changed** (generated posts with
  captions/`image_ref`; the published post's full state) so a call is self-describing —
  removes the follow-up query after each of the ~140 trigger hits.
- Optional: a one-shot `POST /dev/run-cycle` (generate → auto-approve → publish-next) to
  exercise the whole loop in a single call instead of 50 separate pokes — dev/test only,
  never a prod path (approval gate stays load-bearing).
- Optional Telegram `/status` summary ("3 suggested, 2 approved, 1 published today") on the
  same query.
- Logs already exist (M2/M3); a one-line per-stage summary is minor next to the status
  endpoint.

**Recommendation.** This is a bigger real win than I first scored it — the endpoint + richer
dev responses would have removed essentially all the manual SQL and most of the sleeping in
that session. Do `/dev/status` and the richer trigger responses together; consider
`/dev/run-cycle` if end-to-end verification stays common. Still "small" effort, now
evidence-backed as high-value.

---

### Suggested sequencing — **M5: post-v1 usability**

None of these block v1 (M4 finishes the ship). Group them as a usability milestone, do the
cheap high-value ones first:

1. **#3 brand → Markdown** (trivial, pure win) — do immediately.
2. **#5 status endpoint + document dev triggers** (small) — removes the daily friction.
3. **#1 random + approved-dedup images** (small) — with the "grow the library" caveat.
4. **#2 reversible decisions** (medium) — scoped to pre-publish, published-state guarded.
5. **#4 schedule control** (env now / Telegram later) — largest; defer the Telegram version
   unless env editing proves painful.

Open decision for the user before an M5 plan: for **#4**, is editing an env var + redeploy
acceptable, or is live Telegram control actually wanted (which pulls in message-handling +
persistence)?
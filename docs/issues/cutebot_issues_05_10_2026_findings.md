# Findings — `cutebot_issues_05_10_2026.md`

Reviewed 2026-10-05 against `main` @ `f95cd0d`. Each item: what exists today, what the
fix actually is, and where it belongs on the roadmap.

**Suggested order:** 7 + 8 (hours) → 3 (an hour) → 6 (when you run the eval) → 1 (with
M8, it's the best training signal) → 4 (M9) → 5 (spike only).

---

## 1. "Fix text" — approve with a wording/spelling edit

**Today:** nothing. This is roadmap item **C — Inline editing** (PRODUCT_SPEC §8,
ROADMAP "Later"), already identified as "a stronger training signal than approve/reject".

**Smallest working version (stateless, ~half a day):**
- Third inline button `✏️ Fix` next to Approve/Reject.
- Tap → bot sends `Reply to this message with the corrected Hebrew caption (post #42)`
  using Telegram `ForceReply`. The post id rides in the prompt text, so there is no
  "awaiting edit" state to store — `process_message` just checks `reply_to_message` and
  parses `#<id>`.
- Reply → `caption_he` replaced, status → `approved`, `Feedback` row stores
  `original → edited`. Prefix the reply with `en:` to fix the English instead.
- Re-send the card via the existing `mark_decided` so you see the final text.

**Skip:** diff view, partial edits, editing after approval. Add when the edit log shows
you actually need them.

**Roadmap:** fold into **M8 (learning loop)** — edit deltas are exactly the few-shot
material M8 wants, and the Telegram plumbing is M6's, already in place.

**feedback** - good, approved
**→ Re: "fold into M8". Noted as an M8 item in the Decisions section below.**

## 2. Approved photos must not be reused

**Today — already true in the DB, not on disk.** `app/stock.py:select_images` excludes
every image attached to an `approved` / `publishing` / `published` post and picks from the
unused set. The **one real gap**: when unused images run out it silently falls back to
reusing committed ones (`stock.py:55-57`).

**Don't move files.** `image_ref` is the path stored on the post, and `GET /media` serves
the file from `stock/` at **publish time** (Instagram fetches it by URL). Moving an
approved-but-not-yet-published image to `approved/` breaks its publish. The DB is the
ledger; folders would be a second, drifting one.

**Fix (small):**
- Remove the fallback: return fewer images and DM `Stock library exhausted — upload more
  photos` (photo upload over Telegram already exists, `_handle_photo_upload`).
- Add `stock: 4 unused / 12` to `/status` so you see it coming.

**If you want physical separation for your own browsing later:** a one-off script that
moves images of `published` posts to `stock/used/` (enumerator already ignores
subfolders — it only reads the top level). Not now.

**feedback** - ok if DB handles it its good for now
**→ Re: "Don't move files / the DB is the ledger". Kept as-is. The two small fixes (drop the exhausted-stock fallback, show `unused / total` in `/status`) ship together with item 9 — same file, same session.**

## 3. Bot comfort — menu, buttons, discoverable actions

**Today:** six slash commands (`/status /generate /postnow /queue /requeue /pending`),
no `/help`, and Telegram's "/" menu is empty because the bot never calls `setMyCommands`.

**Fix (~1 hour, native Telegram features, no new deps):**
- Call `setMyCommands` once at startup → Telegram shows the blue **Menu** button with
  every command + description. This alone solves "see all possible actions".
- `/help` — same list as text.
- Optional: a persistent `ReplyKeyboardMarkup` with `📊 Status · 📋 Pending · 🗓 Queue ·
  ✨ Generate` so the common ones are one tap. Only if the Menu button isn't enough.

**Skip:** conversational flows / wizards. The commands + `/help` cover it.

**feedback** - approved
**→ Re: "`setMyCommands` + `/help`". Scheduled first in the Decisions section — smallest, felt daily. Reply keyboard stays optional.**

## 4. Carousel posts (plan later)

**Today:** in the backlog, deferred out of M7 (ROADMAP "Backlog"). The deferral note
asks for a `post-images` table.

**What it actually needs (M9-sized):**
- Storage: a JSON list column `image_refs` on `Post` — not a join table. One migration,
  `image_ref` stays as the cover. (ponytail: a join table only earns its place when
  images need per-slide captions or reordering.)
- Generation: pick 3–5 unused images, one vision call with all of them, prompt asks for
  a connecting story + one caption; `select_images` already supports `n`.
- Review: Telegram `sendMediaGroup` for the images — media groups **cannot carry inline
  buttons**, so the Approve/Reject card is a second message. Minor rework of
  `send_suggestion`.
- Publish: IG Graph carousel = N child containers → one `CAROUSEL` container → publish.
  Existing container-resume/idempotency logic in `publishers/instagram.py` extends to it.
- Brand file: a short `## Carousels` section with 2–3 sample stories. Do this first, by
  hand — the prompt can't be written without knowing what a good one looks like.

**Roadmap:** **M9**, after M8. Ticket when you get there, not now.

**feedback** - approved, write into project docs
**→ Re: "M9, after M8". Will go into `ROADMAP.md` as a new **M9 — Carousel posts** section (the bullets above, verbatim) replacing the one-line backlog entry, and the "Carousels" brand-file sample becomes M9's first task. Done as part of the item-7 docs pass.**

## 5. Generated media from designated designs (plan later)

**Today:** roadmap item **B — image generation**, marked "deferred for the foreseeable
future". The ask here is bigger: image-to-image from your design + short video.

**Honest assessment:**
- *Story text* — trivial, the LLM does it already.
- *Image with your design in a scene* — feasible with an image-to-image model that takes
  a reference image (design consistency is the hard part; expect ~1 in 4 usable).
- *Short clip* — video models are expensive per second and keep a specific shirt print
  consistent across frames unreliably today. Not worth building a pipeline for yet.
- Both would be the first non-stock media; the stock-library rule in PRODUCT_SPEC §3 and
  the brand "real details, not stock photos" direction need an explicit decision.

**Recommendation:** no milestone. A **half-day spike** when you want it: 10 generations
from one design with a reference-image model, you judge them in Telegram. If >30% are
postable, scope a milestone; if not, park it another quarter. Order: after carousel.

**feedback** - accepted, keep in backlog witha note to wait for better and cheaper future models.
**→ Re: "no milestone, half-day spike when you want it". Stays in `ROADMAP.md` Backlog as "Generated media from brand designs (image + short clip)" with the note: *wait for cheaper, more design-consistent image/video models; re-check at each model-eval round*. No spike scheduled until then.**

## 6. Model comparison is outdated

**Today:** `scripts/eval_models.py` compared `claude-sonnet-4-6`, `claude-opus-4-8`,
`gpt-5.1`. Current Anthropic lineup is **`claude-sonnet-5-5`** ($2/$10 per MTok) and
**`claude-opus-5-5`** ($4/$20) — both newer *and cheaper* than what lost the eval.

**Two things to check now:**
- **Local `.env` has `DEFAULT_LLM_MODEL=openai/gpt-4o`**, not `gpt-5.1`. Confirm the
  Railway variable is the actual winner — the eval decision may not be what's running.
- `litellm` must know the new model ids; if it 400s, bump the pin (see global memory
  `litellm-gpt5-max-completion-tokens` — old litellm can't remap unknown ids).

**Fix:** update the default list in `eval_models.py` to
`anthropic/claude-sonnet-5-5, anthropic/claude-opus-5-5, openai/gpt-6-sol, openai/gpt-6-luna`,
re-run, review the HTML. Make it a standing task: re-run after each provider release,
not "in the future". Record the outcome in DEV_GUIDELINES (its "Model decision" block
still says *outcome pending*).

Candidate set and list prices (per MTok in / out):

| Model | LiteLLM id | In | Out |
|---|---|---|---|
| Claude Sonnet 5.5 | `anthropic/claude-sonnet-5-5` | $2 | $10 |
| Claude Opus 5.5 | `anthropic/claude-opus-5-5` | $4 | $20 |
| GPT-6 Sol | `openai/gpt-6-sol` | $2 | $10 |
| GPT-6 Luna | `openai/gpt-6-luna` | $0.10 | $0.50 |

GPT-6 Sol/Luna shipped 2026-09-22 ([OpenAI](https://openai.com/index/introducing-gpt-6-sol-and-luna/),
[TechCrunch](https://techcrunch.com/2026/09/22/openai-launches-gpt-6-sol-and-luna/)).
`litellm==1.57.3` predates all four ids. Routing is by the `openai/` / `anthropic/`
prefix and the token cap is passed explicitly, so it should just work; bump the pin
only if a candidate 400s.

**Cost per post in the eval:** `eval_models.py` records only elapsed seconds. Add
`response.usage` (input/output tokens) × the price table above per call, and put
`cost: $0.0042 · 1.9k in / 310 out` in each model's header in both the `.md` and the
HTML page. Also a per-model total at the top so the 10-image bill is one glance. ~20
lines in `eval_models.py` + the header template in `eval_to_html.py`.

**feedback** - update to the current models both for claude and for gpt, use sonnet,opus,luna,sol.
include in the comparison a header stating the cost of generating the post
**→ Re: "update the default list" and the cost header. Done above: model list is now Sonnet 5.5 / Opus 5.5 / GPT-6 Sol / GPT-6 Luna with verified ids and prices; cost-per-post header added to the fix (needs `usage` captured per call — the script doesn't today). Dropped Gemini from this round since you named the four; add `gemini/...` later if wanted.**

## 7. Docs after the Instagram smoke test

The smoke test closes M7. Stale spots:
- `ROADMAP.md` **M7.6** still unchecked; mark done with the date, close **CUT-61**.
- `M7_PLAN.md` at root → `docs/archive/` like M0–M6.
- `DEV_GUIDELINES.md` "Model decision — outcome pending" → record the winner (and the
  rule there says: if not Claude, update PRODUCT_SPEC §3 + CLAUDE.md + config default).
- `CLAUDE.md` / `PRODUCT_SPEC.md` / `README.md` still describe the runtime as Claude and
  publishers as stubs → "Instagram live, TikTok/X stubbed".
- `docs/M7_INSTAGRAM_RUNBOOK.md` §5: add "smoke verified 2026-10-05, test post deleted".
- `env.example`: comment the GPT option next to `DEFAULT_LLM_MODEL`.

**feedback** - approved
**→ Re: the stale-docs list. Will do as one hygiene commit together with item 8; adds the M9 carousel section (item 4) and the backlog note (item 5) in the same pass.**

## 8. Organise project files

Root today: 7 `.md` files + `eval_*.html/md`, `cutebot_description.txt`, this issues
file, `cutebot.db`. Proposed layout:

```
README.md CLAUDE.md PRODUCT_SPEC.md ROADMAP.md DEV_GUIDELINES.md SCRIPTS_REFERENCE.md
brand.md brand.example.*                      # stays at root — tooling reads them
docs/
  archive/        M0–M7 plans (move M7_PLAN.md)
  issues/         cutebot_issues_05_10_2026.md + this file; M5_ISSUES.md from archive
  M7_INSTAGRAM_RUNBOOK.md  POST_V1_REVIEW.md
eval/             eval_results.md/.html, eval_comparison_shareable.html (gitignored as
                  a folder, one line instead of three patterns)
```

- `cutebot_description.txt` (13 lines, gitignored) → fold into README or delete.
- Update `CLAUDE.md` "`M<N>_PLAN.md` at repo root" → `docs/plans/<ticket>.md` to match the
  global convention; the active plan is the only one at a predictable path.
- Everything else (`.db`, caches) is already gitignored — fine.

**feedback** - approved
**→ Re: the proposed layout. Will apply as-is (git mv, no content changes) in the item-7 hygiene commit. This findings file moves to `docs/issues/` with it.**

## 9. Remove an image from the available stock

**Today:** no way. An image stays eligible until a post using it is approved; a rejected
post frees it again, so a disliked image keeps coming back.

**Fix (small, DB-only — consistent with item 2):**
- New `/reject` reason **"bad image"** is the natural trigger — you're already on the
  reason picker when it happens. Tapping it marks the image as banned, not just the post.
- Storage: a `banned_images` table (`image_ref`, `banned_at`) via one migration;
  `select_images` adds it to the exclusion set. (ponytail: a table, not a folder — same
  reason as item 2, the DB is the ledger and `/media` keeps serving.)
- `/stock` command: lists `unused / used / banned` counts and `/stock unban <name>` if
  you change your mind. Counts also go in `/status` (item 2).

**Skip:** deleting the file, a "browse stock" gallery in Telegram. If you really don't
want the file around, delete it from `stock/` by hand — the enumerator just won't see
it; nothing in the DB breaks for already-published posts (their media URL is no longer
needed after publish).

**Roadmap:** ship with the item-2 fixes — one small "stock hygiene" change, before M8.

---

## Decisions (post-review, 2026-10-05)

| # | Decision | When |
|---|---|---|
| 7 + 8 | Docs hygiene + file reorg (incl. M9 section, backlog note) | now, one commit |
| 3 | `setMyCommands` + `/help` | now |
| 2 + 9 | Drop exhausted-stock fallback, `banned_images` + "bad image" reason, `/stock` | now, one change |
| 6 | Eval round 2: Sonnet 5.5 / Opus 5.5 / GPT-6 Sol / GPT-6 Luna + cost header; bump litellm | next eval run |
| 1 | Fix-text button | M8 |
| 4 | Carousel | M9 |
| 5 | Generated media | backlog, revisit per eval round |

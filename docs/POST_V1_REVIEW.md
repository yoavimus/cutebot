# CuteBot — Post-v1 Review (2026-07-08)

> Full-project review after M5: docs, architecture, code, Claude Code tooling, and
> product UX. **Decision document — nothing here is implemented.** Once we agree on
> direction, the chosen items become the next milestone plan(s) + Linear tickets.

## TL;DR — recommendations

1. **Do a half-day hygiene pass now** (§2) — stale docs, one unresolved "decide before
   v1" item, a likely-dead lint hook, three small prod-operability holes.
2. **Resolve the runtime-model decision** (§3.1) with a tiny eval harness instead of
   opinion — it's been "UNRESOLVED, revisit before M4" since M1 and v1 already shipped.
3. **Next milestone (M6): operator comfort + richer feedback signal** (§6) — Telegram
   commands (`/generate N`, `/queue`, `/requeue`), local-timezone posting slots, and
   one-tap **reject reasons**. Small, immediately felt daily, and starts accumulating
   the training data the learning loop needs.
4. **Start the Instagram Graph API prerequisites in parallel** (§6, M7) — Meta app
   review has weeks of external lead time; kick it off now so the first real publisher
   isn't blocked later.
5. **Learning loop (M8) after M6 data exists** — few-shot from approved posts +
   reject-reason conditioning. Doing it before reject reasons exist wastes the milestone.
6. **All three §1.b usage notes are sound** and have clear roadmap homes (§5.0):
   Telegram commands → M6, style improvement → model eval + M8 (+ a new "brand
   distillation" mechanism from your own idea), multi-image → M7 with the first real
   publisher. §7 takes the longer product view: the real product is a **trust loop**
   (north-star metric: approval rate), and for the Israeli market the next review
   channel should be **WhatsApp**, not Discord/Slack.

---

## 1. Current state — honest snapshot

**The good news: v1 genuinely shipped and the codebase is in strong shape.**

- The full generate → review → queue → publish loop runs autonomously on Railway,
  verified in prod (M4), with the M5 usability round on top.
- ~2,050 lines of app code + tests. `ruff`, `mypy`, `pytest` (39 tests) all green;
  CI runs all three on push/PR.
- The three architectural guarantees the spec calls load-bearing are actually enforced
  in code, each in exactly one place:
  - **Approval gate** — `publish_next` only pulls `APPROVED` posts via `queue.peek_next`;
    reversals hard-block `PUBLISHING`/`PUBLISHED` (`review.handle_decision`).
  - **AI disclosure** — composed only in `render.render_full_caption`, used by both the
    notifier and the publishers; tests assert its presence.
  - **Idempotent publish** — status flips to `PUBLISHING` before any network call;
    `recover_orphaned` sweeps on startup.
- Deliberate shortcuts are marked with `ponytail:` comments naming their ceiling
  (peek→claim not atomic → fine single-process; startup sweep → fine until
  multi-worker). This is exactly how v1 shortcuts should be recorded.

**The honest caveat:** with stub publishers, CuteBot today is an *approval-gated
caption generator* — the owner still hand-posts to the actual networks. The product
thesis ("autonomous pipeline") isn't proven until the first real publisher (M7 below).

---

## 2. Hygiene backlog (cheap, do as one batch)

| # | Item | Where | Why |
|---|------|-------|-----|
| H1 | README says "Status: 🚧 Skeleton / scaffold… LLM generation and publishing are functional stubs" | `README.md` | v1 is shipped and generation is real. First thing any visitor reads is false. |
| H2 | `PRODUCT_SPEC.md` §1.b is a scratch wishlist inside the "source of truth" (also a stray `n` typo on the last line) | `PRODUCT_SPEC.md` | Ideas belong in ROADMAP/backlog; the spec should only state decided scope. The three §1.b items are triaged in §5 below. |
| H3 | M0 checkboxes all unchecked though M0 shipped | `ROADMAP.md` | Cosmetic, but the doc contradicts itself. |
| H4 | `M4_PLAN.md` still at repo root; M0–M3, M5 are archived | root → `docs/archive/` | Convention drift. |
| H5 | `env.example` prod section says `BRAND_FILE=/data/brand.yaml` | `env.example` | Stale since M5.1 (now `brand.md`). If prod Railway still points at `/data/brand.yaml`, the live brand file missed the rename too — verify. |
| H6 | Live `brand.md` content is still YAML-formatted | `brand.md` | Works (injected verbatim), but M5.1's whole point was non-tech-editable prose. Rewrite it in the `brand.example.md` style once; also the "Sample posts" section is where few-shot examples will live (§5.2). |
| H7 | `*Zone.Identifier` junk files in `stock/` and root | WSL artifacts | Delete; add `*Zone.Identifier` to `.gitignore`. |
| H8 | Webhook secret has a guessable default (`cutebot-webhook-secret`) | `app/config.py` | Prod sets a real one, but nothing *enforces* it. One startup check: refuse to boot in prod with the default secret. |
| H9 | **Model decision still marked "UNRESOLVED… pick one before shipping v1"** | `DEV_GUIDELINES.md` | v1 shipped; the flag is now overdue. Resolve via §3.1, then update DEV_GUIDELINES + spec. |

---

## 3. Dev POV — architecture, risks, tooling

### 3.1 The one overdue decision: runtime model

M1 found GPT-4o clears the Hebrew gate while the coded default stays
`anthropic/claude-sonnet-4-6`, and the guideline said to decide before v1. The user's
own §1.b note ("improve bot writing style… will a better model improve?") says style
is still the felt pain, so decide with data, not vibes:

- **Build a ~50-line eval script** (`scripts/eval_models.py`): run the same N stock
  images × brand file through 2–4 candidates (Sonnet 4.6, Opus 4.8, GPT-4o — model IDs
  via the `claude-api` skill at build time), dump captions side-by-side to one Markdown
  file, native-speaker review picks a winner. One session of work; the LiteLLM seam
  makes the sweep trivial (`DEFAULT_LLM_MODEL` is already the only switch).
- Record the outcome + cost/quality tradeoff in DEV_GUIDELINES, update the spec if the
  winner isn't Claude, delete the "UNRESOLVED" block.
- Keep the eval script — it's also the harness for measuring the learning loop later
  (same inputs, prompt-with-few-shot vs without).

### 3.2 Real gaps worth fixing (found in code review)

These are prod-relevant, not theoretical:

1. **A `FAILED` post is unrecoverable in prod.** The only requeue path is
   `POST /dev/requeue/{id}`, and all `/dev/*` routes 404 in production — so a publish
   failure in prod requires manual DB surgery (`railway connect Postgres`). Fix lands
   naturally as a Telegram `/requeue` command in M6.
2. **A lost review DM orphans the post forever.** `send_suggestion` never checks the
   Telegram API response (`telegram.py:58-84`); if `sendPhoto` fails (bad image, API
   hiccup, chat_id typo), the post sits `SUGGESTED` with no buttons anywhere and no
   resend path. Fix: log non-ok responses + a `/resend` (or `/pending`) Telegram
   command that re-DMs undecided posts. Also covers "notification got buried in chat".
3. **A deploy that straddles a posting slot silently skips it.** APScheduler's default
   misfire grace is ~1s and Railway redeploys restart the process; there's no catch-up.
   Cheap fix: generous `misfire_grace_time` on the posting jobs, or a startup check
   "was there a slot in the last hour with a non-empty queue? → publish now".
4. **Posting slots are UTC-only; the owner thinks in Israel time.** Twice a year DST
   silently shifts every publish time by an hour. Cheap fix: a `SCHEDULE_TZ` setting
   (default `Asia/Jerusalem`) passed as the `CronTrigger` timezone — APScheduler
   handles DST natively. This is a papercut that *will* bite in October.

### 3.3 Known ceilings that are fine to leave (explicitly not recommending)

- Single-process scheduler, no Redis/worker — correct for one brand; revisit only at
  multi-tenant (G).
- Peek→claim not atomic, startup-sweep recovery — fine single-process; the `ponytail:`
  comments already name the upgrade path.
- Sequential vision calls in `generate_batch` (~5 × a few seconds, once a day) — not
  worth parallelizing.
- Suggested-post pileup (undecided posts accumulate) — harmless; revisit only if the
  `/pending` command (§3.2.2) shows it's annoying in practice.

### 3.4 Claude Code setup & workflow

What exists: project `CLAUDE.md` (tight, accurate), `run` skill, project memory (4
entries, current), Linear team (CUT-), `settings.local.json` with permissions + hooks,
CI. Overall a healthy setup. Three improvements:

1. ~~The PostToolUse lint/type hooks are almost certainly dead.~~ **Resolved
   2026-07-08:** the hooks parsed a `$TOOL_INPUT` env var that Claude Code never sets
   (hook input arrives as JSON on stdin), so they had no-opped silently since day one.
   Deleted; lint/type enforcement stays with the real gates
   (`ruff check . && mypy app && pytest` + CI).
2. **Shared config is trapped in `settings.local.json`** (gitignored). Move the
   permissions + (fixed) hooks to a committed `.claude/settings.json` so the setup
   survives machine changes; keep only genuinely personal bits local.
3. **CI is missing `alembic check`.** The schema-drift check exists in
   SCRIPTS_REFERENCE but nothing runs it automatically; one CI step catches
   "changed models, forgot the migration" before Railway does.

---

## 4. Docs assessment

The doc system (spec = truth, roadmap = sequencing, plans archived per milestone,
DEV_GUIDELINES = learned rules, SCRIPTS_REFERENCE = commands) is working well —
M5_ISSUES.md is a model of how user feedback → research → milestone should flow.
Two structural notes beyond the hygiene table:

- **The spec needs a "backlog" home.** §1.b happened because ideas had nowhere to go.
  Suggestion: keep a `## Backlog (untriaged)` section at the bottom of **ROADMAP.md**
  (not the spec) — ideas land there, milestones graduate out of it.
- **`cutebot_description.txt` duplicates the README/spec** and mentions Discord as if
  current. Fold anything unique into README and delete it.

---

## 5. Product / UX POV

### 5.0 Triage of the §1.b usage notes (do they make sense? where do they land?)

Verdict: **all three notes are sound** — none conflicts with the spec's principles,
and each maps cleanly onto a post-v1 milestone. They should move out of the spec and
into the roadmap as follows:

| §1.b note | Verdict | Roadmap home |
|-----------|---------|--------------|
| **Telegram: generate a batch now (N as input), pick a post to post now, "maybe more capabilities"** | Yes — the bot is the product's entire UI, and commands are the cheapest leverage available (`process_message` already exists since M5.6). "Post now" must publish only `APPROVED` posts, so the gate is untouched. | **M6** — the operator-console command set (§5.3) is exactly the "more useful capabilities" answer |
| **Writing style must get better** — more examples? better model? a strong model that reviews style and writes guidelines? | Yes — this is the core long-term bet, and all three levers you list are real. They're complementary, not alternatives; sequence them (see below). | **Model eval** (now) → **M8** (few-shot) → **M8/M9** (guideline distillation) |
| **Multi-image posts** | Yes — carousels are *the* format for a fashion brand on Instagram. But each network constrains carousels differently (IG 10 images via Graph API, X 4, TikTok photo-mode rules), so designing it before the first real publisher means guessing. | **M7** — a design input to the Instagram publisher, with schema groundwork (`image_ref` → post-images table) done there |

**On the three style levers specifically:**

- **More examples** — yes, but the highest-value examples are *your own approved
  posts*, not hand-written samples: few-shot selection from accumulated approvals is
  precisely learning-loop A (M8), and it improves automatically forever. Hand-curating
  a few more samples into `brand.md` now is a fine stopgap (pairs with hygiene H6).
- **Better model** — answer it empirically, not by feel: the §3.1 eval harness exists
  for exactly this, and the LiteLLM seam makes trying candidates a one-var sweep.
- **Strong model reviews style and writes guidelines** — this is the most original of
  the three, and worth promoting to a named mechanism: **brand distillation**. A
  periodic (monthly / on-demand `/distill`) job where an Opus-class model reads the
  accumulated feedback — approvals, rejections *with reasons* (§5.2), later edit
  deltas — and proposes a **diff to `brand.md`**, which you approve or reject in
  Telegram like any post. Three properties make it strategically strong: it keeps the
  human in the loop for the brand file itself (same trust model as posts); the learned
  voice lives in a *portable text artifact*, so it survives model switches and doesn't
  lock you into one provider; and it compounds — every cycle sharpens the input that
  every future generation reads. It depends on reject reasons existing, so it slots
  right after M8's few-shot work (same milestone or M9).

### 5.1 Where the friction actually is (user's own signals)

From §1.b, M5_ISSUES, and prod usage, the felt pains rank:

1. **Writing style is "good enough to start" but must get better** — the top product
   pain, and the entire moat. Addressed by §3.1 (model) + §5.2 (feedback signal) +
   few-shot from real approvals.
2. **No on-demand control** — can't ask for a batch now, can't post now; everything
   waits for cron. ("add option for bot to generate batch now, select post to post
   now").
3. **Stub publishing** — the loop ends in a log line; the owner still posts by hand.
4. **Multi-image posts** — real for a fashion brand (carousels are the format there),
   but each network constrains carousels differently, so it's best decided *with* the
   first real publisher, not before.

### 5.2 The feedback signal is too thin to learn from (structural product gap)

`Feedback` stores only approve/reject. When the learning loop arrives it will have
nothing to learn from a rejection — *why* was it rejected? Off-voice? Bad Hebrew?
Wrong image? Boring? Two cheap, high-leverage additions, in order:

- **One-tap reject reasons**: tapping ❌ swaps the buttons for 3–4 reason chips
  (voice / Hebrew / image / boring) + skip. One extra tap, zero typing, and it turns
  every rejection into a usable training signal. Schema: nullable `reason` column on
  `Feedback` (+ migration). *This should ship before the learning loop, or the loop
  starts blind.*
- **Approve-with-edits** (spec roadmap C): reply-to-message with corrected text →
  strongest possible signal (exact delta). Bigger lift (reply handling, edited-caption
  storage); second wave.

### 5.3 Telegram is the product — invest there

The bot is the entire UI. M5's `/status` proved a command is cheap once
`process_message` exists (it does). Natural command set, roughly in value order:

| Command | Covers | Notes |
|---------|--------|-------|
| `/generate [N]` | §1.b on-demand batch | Reuses `generate_batch(n=…)` as-is |
| `/postnow [id]` | §1.b "post now" | No id → front-of-queue; with id → that specific post. Either way only **approved** posts — gate untouched |
| `/queue` | visibility | Ordered approved list with captions preview |
| `/requeue <id>` | prod FAILED gap (§3.2.1) | Same guard as dev route |
| `/pending` | lost-DM gap (§3.2.2) | Re-DM undecided suggestions |
| Photo upload → stock | library growth | Owner DMs a photo, bot saves it to `STOCK_IMAGES_DIR`. Solves "stock library must keep growing" from the phone, where the photos are. Needs owner-chat gating (exists) + file download. |

All owner-gated via the existing chat-id check. Each is small; together they make the
bot feel like an operator console instead of a one-way notification stream.

### 5.4 Deliberately not recommending (yet)

- **Web dashboard** — Telegram + `/status` covers single-tenant operation; a dashboard
  is a multi-tenant-era feature.
- **Schedule editing via Telegram** — user already accepted env+redeploy (M5.5
  decision). The timezone fix (§3.2.4) removes most of the remaining annoyance.
- **Image generation** — spec says deferred for the foreseeable future; nothing
  changes that.
- **Discord/Slack notifiers, analytics, multi-tenant** — all post-real-publisher.

---

## 6. Proposed sequencing (replaces the current post-v1 list order)

The current ROADMAP order is A (learning) → C (edits) → E (publisher). Two problems:
A before reject-reasons learns from thin data, and E has **external lead time**
(Meta business verification + app review can take weeks) that should start ticking
now regardless of build order.

| Milestone | Contents | Size | Why this order |
|-----------|----------|------|----------------|
| **Hygiene batch** (not a milestone) | §2 table + dead hooks + CI `alembic check` | ~half day | Truth debt compounds; do before anything else |
| **Model eval** (standalone task) | §3.1 eval script + decision + doc updates. Round 1: Sonnet / Opus / same-tier GPT (needs Anthropic credits); round 2 adds Gemini | ~1 session | Unblocks "better style" question with data; harness reused later |
| **M6 — operator console & feedback signal** | Full Telegram command set (§5.3, incl. `/postnow [id]` + photo-upload), reject reasons (§5.2), timezone-aware slots, misfire catch-up | small–medium | Daily-felt UX wins; fixes all three prod-operability holes; starts collecting the data M8 needs |
| **M7 — first real publisher** (spec E) | Instagram Graph (fashion brand → IG first): OAuth, media upload, carousel support decision (folds in multi-image, §5.1.4) | medium–large | Makes the product real. **Start Meta app/business-verification paperwork during M6** |
| **M8 — learning loop v1** (spec A) | Few-shot from top approved posts + reject-reason conditioning in the prompt; measure with the eval harness | medium | Now has real signal (M6) and real stakes (M7) |
| Later | Approve-with-edits (C) → Discord/Slack (D) → analytics (F) → multi-tenant (G) | — | Unchanged relative order |

### Decisions (answered by Yoav, 2026-07-08)

1. **Model eval** — DECIDED: native Hebrew review by Yoav. Candidates must be
   same-generation/tier across providers: Claude Sonnet, Claude Opus, the equivalent
   OpenAI model, Gemini. **Credits constraint:** only OpenAI credits on hand today;
   Anthropic credits to be purchased. So **round 1 = Sonnet + Opus + same-tier GPT**
   (blocked on buying Anthropic credits), **round 2 adds Gemini** and others later.
   The eval script should make adding a candidate a one-line list change (the LiteLLM
   seam already gives us this); exact model IDs resolved via the `claude-api` skill at
   build time.
2. **M6 scope** — DECIDED: **full command set** from §5.3, including photo-upload.
3. **First network** — DECIDED: **Instagram**. Meta paperwork starts during M6.
4. **Reject reasons** — DECIDED: the four chips (voice / Hebrew / image / boring) + skip.

**Additional note (Yoav):** `/postnow` should also take an optional post id —
`/postnow` publishes front-of-queue, `/postnow <id>` publishes that specific
**approved** post immediately. Sound and cheap: same publish flow, selection by id
instead of `peek_next`, and the same hard guard (only `APPROVED` posts; anything else
answers "can't — post is <status>"). Folded into the §5.3 command table and M6 scope.

---

## 7. The product as a product — longer-term view

Everything above optimizes the current loop. This section asks what CuteBot *is* once
the loop works.

### 7.1 The real product is trust, not captions

Caption generation is a commodity — any tool with an LLM key writes captions. What
CuteBot uniquely builds is a **trust loop**: a system that learns one brand's voice
from lightweight human decisions until supervision cost approaches zero. The product
arc is:

```
assistant ──▶ apprentice ──▶ trusted autopilot (owner-controlled)
 (review        (approval        (weekly digest; auto-publish
  everything)    rate climbs)     only where the owner dials it up)
```

This reframes the approval gate from a constraint into a **progression system**. The
gate stays load-bearing forever — but "graduated autonomy" becomes an owner-controlled
dial, e.g. "publish without asking when nothing is unusual; ask when unsure; send me a
weekly digest instead of daily DMs". Never a default, always an earned, reversible
owner choice. That is the endgame feature everything else feeds.

**North-star metric: approval rate** (approvals / decisions), computable *today* from
the `Feedback` table. Secondary: owner-minutes per published post. If the learning
loop works, approval rate trends up and owner time trends down — if those numbers
don't move, the moat isn't materializing and we should know early. A `/stats` command
(or extending `/status`) makes the flywheel visible to the person feeding it.

### 7.2 Feedback flywheel — maturity ladder

Each level multiplies the value of the accumulated data; the roadmap should climb them
in order:

| Level | Signal | Status |
|-------|--------|--------|
| L0 | approve / reject | ✅ shipped (v1) |
| L1 | + reject reasons | M6 (§5.2) |
| L2 | + edit deltas (approve-with-edits) | post-M8 (spec C) |
| L3 | + audience engagement (likes/reach per post) | spec F, needs real publisher first |
| L4 | + **brand distillation** — feedback compiled back into `brand.md` (§5.0) | M8/M9 |

L3 is the qualitative jump: everything below it learns *the owner's taste*; L3 learns
*the audience's response*. A post the owner loved that flopped is a signal no amount
of approve/reject can produce.

### 7.3 From reactive captioning to an editorial brain

Today generation is **memoryless**: each batch captions images with no awareness of
what was posted last week. That caps quality regardless of model. Cheap-to-real
progression:

- **Recent-post memory** (cheap, near-term): inject the last N published captions into
  the prompt with "don't repeat these themes/jokes". For a voice built on a running
  gag (the "פנו אלינו בפרטי" signature), variety-awareness is the difference between
  a bit and a rut. Could slot into M8 alongside few-shot.
- **Calendar awareness**: Jewish holidays, sales seasons, brand dates — a small
  events file injected when a date is near. High leverage for the Israeli market;
  purely a prompt change.
- **Campaign arcs** (later): "product launch = teaser → reveal → behind-the-scenes"
  as multi-post plans the owner approves as a unit. This is where per-post approval
  UX evolves into approving a *plan*.

### 7.4 Market position (if this becomes more than one brand)

- **Hebrew-first is the wedge.** Generic tools (Buffer/Later/Canva AI) produce
  translated-feeling Hebrew; CuteBot treats native Hebrew as a hard gate. The Israeli
  SMB market is underserved and reachable — that's a defensible niche, not a feature.
- **Chat-native review is the UX moat.** Competitors put review in a web dashboard;
  CuteBot lives where the owner already is. Which implies: for Israeli SMBs the next
  notifier isn't Discord or Slack — it's **WhatsApp Business**. Recommend re-aiming
  roadmap item D at WhatsApp; the notifier interface makes this a pure adapter, but
  the WhatsApp Business API has its own approval process (like Meta's Graph API), so
  its paperwork also has lead time.
- **Pricing anchor** (H2, when multi-tenant): the alternative for a small brand is a
  freelance social manager at ₪1–3K/month. A tool that delivers 80% of that for a
  fraction of the price has obvious room; per-brand marginal cost is a handful of
  vision calls a day (agorot, not shekels).
- **Onboarding = the distillation engine in reverse** (H2): the same "strong model
  writes the brand file" mechanism (§5.0) can *interview* a new owner in chat and
  draft their initial `brand.md` from the conversation + their existing posts. The
  hardest onboarding artifact writes itself; day-one voice quality comes from the
  interview instead of weeks of feedback.

### 7.5 Honest risks

1. **Platform gatekeeping** — Meta/TikTok/WhatsApp APIs all have review processes and
   revocable access; this is the biggest external dependency, and why paperwork
   should start before the code that needs it (§6).
2. **Stills-only ceiling** — reach on IG/TikTok is short-form video; a fashion brand
   without Reels has a capped audience. Templated slideshows/Reels from existing stock
   photos is the pragmatic exploration (H3) — real video generation is not on any
   horizon here.
3. **AI-content backlash** — the cute mandatory disclosure is already the right
   posture; keep treating it as a brand asset (transparency as charm), never as
   fine print to minimize.
4. **Single-operator fatigue** — 5 suggestions/day = 150 decisions/month for one
   person. If approval rate doesn't climb (7.1), the owner burns out before the
   flywheel spins. Watch owner-minutes as seriously as output quality.

### 7.6 Horizons summary

| Horizon | Theme | Contents |
|---------|-------|----------|
| **H1** (current → months) | Prove the loop earns trust | M6–M8: operator console, real publisher, learning loop, recent-post memory. Exit criterion: approval rate visibly climbing on a real network. |
| **H2** (after H1 holds) | 2–5 pilot brands | Multi-tenant (G), interview-based onboarding, WhatsApp notifier, per-brand config in DB, engagement feedback (F), pricing test. |
| **H3** (deliberate bets) | Widen the moat | Graduated-autonomy dial, campaign arcs, carousel/Reels media expansion, brand distillation as a headline feature. |

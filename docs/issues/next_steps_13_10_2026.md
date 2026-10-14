# Next steps — feedback on the owner's 9-item list (2026-10-13)

Source: the uncommitted block in `docs/ROADMAP.md` ("i want to re-order and plan the next
steps"). This file is the response; the roadmap stays untouched until you decide. Reply
inline with `**feedback**` lines and I'll run `/notes`.

**TL;DR:** the list is right, the order isn't. Two things change it: (1) **item 4
(multi-brand) is not one milestone — its data seam (a `Brand` row + `brand_id`) should go
first, before 1/2/3 bake in single-tenant assumptions, and its product work (onboarding,
limits, billing) should go late;** (2) item 6 hides a hard fact: **Instagram has no
text-only feed posts** — "posts without images" means *rendering* an image (text card) or a
different anchor, not skipping the image. Proposed order at the bottom.

---

## 1. Better-looking Telegram menu

What Telegram actually offers, cheapest first:

- **Reply keyboard** (`ReplyKeyboardMarkup`) — persistent buttons under the input box:
  `📊 Status · ✨ Generate · 📬 Queue · 📷 Stock · ⚙️ Settings`. One-tap, no typing, no
  memorising commands. Half a day; it's a `reply_markup` on `/start` and `/help`.
- **HTML formatting + grouping** in `/help` and `/status` (bold headers, monospace
  counts, `—` separators). An hour; today's output is plain text.
- **Inline keyboards for drill-down** (`/queue` rows with `▶ post now` / `✏️` buttons,
  `/stock` with `🚫 ban`) — the pattern ✏️ Fix already uses.
- **Mini App** (a web page inside Telegram, served by our FastAPI app, auth via the
  `initData` HMAC) — the only route to a *real* UI: forms, galleries, toggles. Worth it
  only once item 2 (settings) or item 3 (media gallery) need a form. Don't start here.

**Suggestion:** do the first three now as a 1–2 day polish ticket; make the Mini App a
decision inside items 2/3, not a goal of its own.

## 2. Schedule from the bot · learning mode · the "almost" signal

Three separate things; all good; all small.

**a. Schedule from the bot.** Today `GENERATION_CRON` / `POSTING_SLOTS` are env vars →
edit + redeploy. Fix: a `settings` table (key/value, per brand once item 4's seam exists;
env stays as the default) + `scheduler.reschedule_job()` at runtime — our jobs already
have stable ids and `replace_existing=True`, so this is a 1-migration, ~100-line change.
Bot surface: `/schedule` (show), `/schedule post 12:00,18:00`, `/schedule generate 09:00`,
`/schedule pause|resume`. Time-pickers are the first Mini-App temptation — resist; text
`HH:MM` lists are fine for one owner.

**b. Learning mode ("approve but don't post").** Model it as a **new status, not a flag**:
`approved` *is* the publish gate (DEV_GUIDELINES rule 2), so learn-only posts must never
carry that status. Add `PostStatus.KEPT` (approved for learning only): ✅ writes a normal
approve `Feedback` row, post goes to `kept`, never enters the queue, and `learning._LIVE`
includes it as a positive example. Two ways to reach it:
- per post — a third button `📚 Keep` next to ✅ / ❌ / ✏️ (always available), and
- per bot — `/mode learning` makes ✅ mean Keep and pauses posting slots; `/mode live`
  restores. This is also the **onboarding mode for a new brand** (item 4): run generation
  at a high cadence for a week, collect 30–50 decisions, then flip to live. Cheap and it
  turns cold-start into a feature.

**c. "Almost good but not postable."** Two observations:
- ✏️ Fix already *is* the "almost" signal when you know the fix — the edit pair is the
  strongest thing the learning context has. Don't duplicate it.
  **feedback** - i am talking about cases where i dont have an idea how to fix the post, but i do want to mark it as pretty good but not enough to post, as opposed to rejected posts that are plain bad.
**→ Same thing, sharper: `🤏 Almost` is a third verdict, not a reject flavour.** Store `Decision.ALMOST`; the post ends `rejected` (never publishable) but learning treats it as a *positive direction with a flaw*: a `<near_misses>` block ("the owner liked this direction but it wasn't postable — learn the direction, don't copy the execution"), ranked above rejects and below approvals. Chips stay optional (`skip` is a chip). Two ideas that make it more than a label:
- **🔁 Retry on Almost** — the same image is re-captioned immediately with the almost'd caption in the prompt as "close, try again". If the retry gets ✅, we now have an automatic before/after pair — the same signal ✏️ Fix produces, *without you typing the fix*. The image stays reserved for the retry instead of returning to the pool.
- **Almost counts as half an approval in `/status`** — "learning: 12 ✅ · 7 🤏 · 20 ❌" tells you (and later a beta brand) how close the model is, which is the number that decides when to flip `/mode live`.
Plan updated: 2c = Almost + optional chips + Retry; near-miss block in learning.



- What's missing is "right direction, wrong execution, I won't type the fix". Model it as
  `Decision.ALMOST` (post ends `rejected`, so nothing publishes) **reusing the reject
  reason chips** (voice / hebrew / image / boring) so the signal says *which axis* missed.
  Learning renders a `<near_misses>` block: the caption + "liked, but: hebrew". One enum
  value, one chip row, one render block. The risk is signal soup — keep it to those four
  axes, no free text.

**Suggestion:** one milestone, three tickets (a, b, c). Do (b) before (a) if a second
brand is near — it's the onboarding tool.

## 3. Media upload & management via the bot

Current state: a photo DM lands in `stock/`. Two things to know:
- Telegram **compresses `photo`** (≤1280 px, re-encoded JPEG). Sending as **file/document**
  keeps the original. IG wants ≤1440 px wide anyway, so photos *work*, but `/help` should
  say "send as file for full quality". Accepting `document` + **media groups** (albums,
  up to 10 per message) is a day of work and covers 90% of a small brand's upload needs.
- Management in chat is painful past ~20 images. `/stock` with paging + thumbnails +
  inline `🚫`/`🗑` buttons is the chat ceiling; beyond it, a **Mini App gallery** (grid,
  ban/unban/delete, "used 3× / last 2026-10-01") is the first thing that justifies the
  Mini App.

"SOTA standard ways" for small brands = **a shared Google Drive folder**. The owner drops
files where they already keep them; a nightly job pulls new ones (Drive API, one OAuth or a
service account with the folder shared to it). That's the bulk path; Telegram stays the
"quick add" path. Dropbox is the same shape. Object storage (S3/R2) is an *internal* need
that arrives with item 4 — a Railway volume is one service, one disk; `image_ref` is
already an opaque key, so swapping the backing store later is contained in `app/stock.py`.

**Suggestion:** now — documents + albums + `/stock` paging; with item 4 — move files to
R2/S3 keyed by brand; later — Drive folder sync as the first "integration".

**feedback** i am using google drive or google photos for my photos i want to post, so an integration where the railway pulls stock to the volume from a brand photo album seems niceer than s3 that most untechnichal brand managers wont know what s3 is. is this what you suggested or is it different and what do you think about it ?
**→ Yes, that's what I meant — Drive is the user-facing path; S3/R2 was only ever the internal backing store (nobody sees it), and it's not needed while one Railway volume is enough. Two facts change the details:**
- **Google Photos album sync is no longer possible.** Since 2025-03-31 the Photos Library API only returns media *the app itself uploaded*; third-party apps cannot read a user's albums any more. The replacement, the **Picker API**, is a manual flow: we create a session, the owner opens a Google Photos link, taps the photos, and we download the selection. That is still a usable *quick-add*: `/import` → link → pick 20 photos in the Google Photos UI → bot replies "20 photos added". Per batch, not continuous sync.
- **Google Drive is fully supported and dead simple for a non-technical owner:** they share a folder with the bot's service-account email (one "Share" tap, no OAuth consent screen), and a nightly job + `/sync` pulls new files to the volume. Creative extra: **write status back into their Drive** — move posted images into a `posted/` subfolder and banned ones into `banned/`, so the owner's own folder shows what's left and is the management UI, no Mini App needed.
Suggested order for item 3: Telegram files/albums (quick add) → **Drive folder sync (bulk + status write-back)** → Photos Picker `/import` (nice-to-have, only if a brand lives in Google Photos and won't use Drive). Plan updated.


## 4. Serving other brands

The biggest item and the one that reshapes every other one. Split it:

**4a. Tenant seam (do this *first*, it's the cheapest part).** A `brands` table
(`id, name, telegram_chat_id, brand_text, stock_prefix, mode, created_at`) and `brand_id`
on `posts`, `batches`, `feedback`, `banned_images`; `chat_id → brand` lookup in the
webhook; **`brand.md` moves into the DB** (`brand_text`; `/distill` edits the row, the
`.bak` becomes a `brand_text_history` row or just the previous value column). Migration
creates one default brand from the current env and backfills. Nothing visible changes.
Why first: items 2 (per-brand schedule/mode), 3 (per-brand stock), 6 (per-brand anchors)
and 7 (per-brand metrics) all need the key; built single-tenant they get rewritten.

**4b. Product work (do late):**
- **Onboarding wizard** in the bot: unknown chat sends `/start` → name → brand voice (paste
  text, *or* answer 5 questions and let the model draft `brand_text`) → upload 10 photos →
  connect Instagram → pick schedule → starts in learning mode (item 2b).
- **Instagram per brand**: your Meta app is already approved for `instagram_content_publish`;
  other brands connect via Facebook Login (OAuth) and we store *their* long-lived token
  per brand (encrypted at rest). Token refresh job (60-day expiry) becomes mandatory.
- **Usage limits**: posts/month and LLM $/month per brand. We already get `tokens_in/out`
  per call — persist `cost_usd` on `Batch`/`Post` now (one column, trivial) so the data
  exists when limits arrive.
- **Billing**: not until a second brand asks to pay. Manual invoicing for brand #2–5.
- **Ops**: one bot, many chats is fine; one Railway service is fine to ~20 brands; the
  volume → R2/S3 move (item 3) is the real scaling step.

  **feedback** all initial brands will be friend and beta testers so the usage limits billing etc can indeed be later
**→ Agreed — dropped from the plan. Keeping only one line of prep: `cost_usd` on `Batch`/`Post` (we already have the token counts), so you know what each beta brand costs you in API money. Everything else in 4b shrinks to: onboarding wizard + per-brand Instagram connect + learning mode by default.**
**feedback** i ddidnt mean to drop it just to clarify about the urgency of these matters.
**→ Understood — restored as a late milestone (M16 in the table: usage caps per brand, cost view in `/status`, manual invoicing first, payment provider only when a brand asks to pay). `cost_usd` still lands early so the numbers exist by then.**



## 5. Another posting channel: X

The `Publisher` interface makes the adapter itself ~150 lines. What's actually expensive:
- **X API access** — the free tier has been write-only with a low monthly post cap and
  the paid tiers start around $100/month; image upload still goes through the v1.1 media
  endpoint with OAuth 1.0a user context. Verify current pricing before committing — it has
  changed several times.
**feedback** whats the free tier post cap? maybe we can live with that
**→ There is no free tier any more.** X closed Free/Basic/Pro to new developers on 2026-02-06 and moved to **pay-per-use: ≈ $0.015 per post, $0.20 per post that contains a link, $0.005 per read** (Basic was $200/mo, Pro $5,000/mo — legacy only; verify on docs.x.com before budgeting). For a brand posting daily that's **~$0.50/month** — the API is effectively free; the cost is engineering (OAuth 1.0a user context, media upload endpoint) and a design rule: **no links in X posts** (13× the price) — link in bio instead. So "can we live with it": yes, financially trivially. Per your note below, parked until a brand asks.


- **Per-network captions** — today one caption (he + en + disclaimer) goes everywhere.
  X's 280 chars won't hold it. That's a schema change (`caption_{network}` or a
  `post_variants` table) and a prompt change — the real work of any second network.

**Insight:** for an Israeli small brand, X is low reach. **A Facebook Page is the cheap
second network** — same Meta app, same Graph token flow, `POST /{page-id}/photos` with a
page token, no new API vendor (one extra review scope, `pages_manage_posts`). After that
TikTok (video — out of scope while we're image-first) and **WhatsApp Status/Channels** are
more Israeli-market-relevant than X. Suggest: Facebook Page first, X when a brand asks.

**feedback** - facebook is a dead app that nobody uses, tiktok is great but out of scope. whatsapp is great for conversations but not for posts. but you know what lets start with IG until a brand wants a different platform
**→ Agreed: Instagram only until a brand asks.** Second-network milestone removed from the plan; X goes to the backlog with the pay-per-use note above (cheap per post, the work is per-network captions + media upload). One consequence worth stating: with IG-only, "posts without media" (item 6) has **no outlet for now** — see my answer there.


## 6. Posts without an image anchor — this needs the most thought

The image-first rule exists for a reason: an anchor stops the model writing generic
marketing mush. Removing the image means **replacing the anchor, not dropping it**. And
one hard constraint up front: **Instagram feed posts require media** — a "text post" on IG
is a *rendered text card* (Threads, X, Facebook and WhatsApp allow true text). So item 6
is two things:

**feedback** - i wrote this thinking about X posting not instagram, i think the media-less anchors will not work on instagram so this will be a feature only to platform where its relevant
**→ Right, and that resolves item 6 cleanly:** on IG the anchor *steers* an image post; media-less generation only switches on with the first text network (X). So item 6 for now = **anchors as steering**: `/idea winter collection arrived` + a stock image → the caption is about the idea *and* grounded in the photo. That's the thing a copywriter does when you tell them "mention the new collection". The text-card renderer stays as an optional IG route for a brand that wants quote posts — not on the critical path. Plan updated.


**6a. Anchors other than an image** (ranked by signal quality, cheapest first):
1. **`/idea <text>` from Telegram** — "new winter collection arrived", "we're closed on
   Sukkot", a customer quote. One `anchors` table (`brand_id, kind, text, used_at`),
   generation consumes one per post. Highest-signal anchor there is; it's what a human
   copywriter asks the owner for. Could ship in a day.
2. **Calendar anchors** — Israeli/Jewish holidays, seasons, Friday/weekend, brand dates.
   Computable (`hdate`/`convertdate` libs), timely, and exactly what a small Israeli brand
   posts about anyway. Zero owner effort.
   **feedback** - good suggestion i like it. make it an option but not the default
**→ Done: per-brand setting, default off.** Better than "on/off": when on, the bot **asks a week ahead** — "🕎 Hanukkah in 10 days — want 2 posts? ✅ / ❌ / only this year" — and your taps become the brand's calendar preferences (a religious-goods brand says yes to everything; a tech brand to nothing but Friday). No silent holiday posts ever. Also lets you add brand dates: `/date 2026-11-20 our 2nd birthday`.

3. **Content pillars / themes** in `brand_text` — "behind the scenes", "tip", "offer",
   "customer story". Easy, but static lists get repetitive fast; use them as the fallback
   when 1 and 2 are empty, with a "don't repeat the last N pillars" rule like
   `learning_recent`.
   **feedback** - that important knowledge, may the anchored posts will not run indefinetly and will be limited, like how media stock is limiting the media posts we need to have a limit to the media-less anchor posts. suggest limits that are creative and not just a arbitraty number
**→ Good catch — media-less posts need a supply, not a quota. Ideas, from most to least natural (lettered so they don't collide with the 6a list, which continues with 4 and 5 below):**
a. **Anchors are consumables, shown in `/stock` next to images.** `/idea` entries, calendar events and brand dates each get used once (or N variations) and then are spent: "stock: 14 images · 3 ideas · 2 dates". When the anchor stock is empty, generation of anchor posts simply stops and the bot asks for more — exactly the low-stock DM images already have. The owner is the supply chain; the model never invents from nothing.
b. **The brand doc as a stock of facts.** Once per brand-text change, extract concrete claims (products, prices, places, materials, origin story, FAQs) into a `brand_facts` list with `used_at`. A theme post **must cite one unused fact**; facts come back after a cooldown. When the facts run out, the model is literally out of material → "I've said everything your brand doc says; tell me something new (`/idea`)". This is the honest limit: *what the brand has actually told us*.
c. **Half-life cooldown for themes.** A pillar ("behind the scenes") can't repeat until K other posts went out, and K doubles each use (2, 4, 8…) — repetition decays naturally instead of a flat "max 3 per month".
d. **Diet ratio as a hard ceiling.** At most 1 in N published posts in a rolling window is media-less (N per brand, default 4) — enforced in the queue, not in generation, so it can never starve image posts.
e. **Performance-pruned (needs analytics).** An anchor kind whose posts underperform the brand's median reach gets its ratio halved automatically; one that outperforms gets room.
I'd ship **a + b** as the model (they *are* stock) with **c** as the theme rule and **d** as the safety ceiling; e later. Plan updated.

4. **Performance anchors** — variations of top-performing past posts (needs item 7).
5. **Model-asked questions** — the bot asks the owner one question a week ("what's the
   most common question customers ask?") and the answer becomes an anchor. Cute, later.

**feedback** - this part is weird looking and also the numbering is confusing 1-2-3-4-5-4-5 ?
check for hellucinations
**→ Not a hallucination — a layout accident: my answer to your note under item 3 was inserted *inside* the 6a list and used its own 1–5 numbering, so the list read 1-2-3-(1-5)-4-5. Fixed by lettering my limits a–e; the 6a anchors are 1–5 as originally written (4 = performance anchors, 5 = model-asked questions, both still "later"). Content checked against the code: `visual_concept`, `learning_recent`, `ig_media_id`, Pillow dep are all real; the only unverified claims in the doc are the vendor facts, which are listed under Sources.**


**6b. The visual for anchor-only posts:**
- **Text card renderer** — Pillow (already a dependency) draws the caption's hook line on
  a brand-coloured card with the logo. Hebrew needs `python-bidi` + a Hebrew font
  (Assistant/Heebo are open). Brand colours/font/logo become brand settings. Solid,
  ~2 days, and it unlocks true "no stock image" posts on IG.
- **Anchor + stock image** — keep image-first but let the anchor steer the choice
  (pick the stock image whose cached `visual_concept` best matches the anchor; we store
  `visual_concept` per post already, so each stock image gets one after first use).
  Cheaper than a renderer, keeps the photo feel, but needs stock that covers the themes.

**Suggestion:** 6a-1 (`/idea`) + 6a-2 (calendar) + 6b "anchor steers image" first — all
reuse existing pieces. The text-card renderer when a brand's stock runs dry or wants
quote posts. The "list of themes" idea is the fallback tier, not the headline.

## 7. Analytics & feedback

Cheapest high-value item on the list, and the proof that the learning loop works:
- We already store `ig_media_id`. A nightly job calls `GET /{ig-media-id}/insights`
  (reach, likes, comments, saves, shares — note `impressions` is being retired in newer
  Graph versions, use `views`) at +1d / +3d / +7d into a `post_metrics` table.
- Learning gets a `<top_performers>` block (best 3 by saves+shares per reach) — a *real*
  positive signal, not just "you approved it".
- A **weekly digest DM**: posts published, best post, reach trend, stock left, pending
  reviews. Owners love this and it costs one query.
- Same table feeds item 4's "usage" view and item 6's performance anchors.

One migration, one scheduler job, one learning block, one digest. Do it before marketing
to other brands — "it learns from what performs" is the pitch.

**feedback** - this is easy to imlement code-wise but whats needs work is me the human to set up a good image stock for the brand and im waiting for the learning loop to make the post suggestions better before i allow it to actually post so this will take time to actually have data.
**→ Agreed, moved to the end.** Nothing is lost by waiting: `ig_media_id` is stored, and IG insights are lifetime metrics fetchable any time after publish — only the +1d/+3d curve is lost for early posts, which doesn't matter. Your real blocker ("waiting for the learning loop before I allow posting") is exactly what item 2b's `/mode learning` + `Keep` solves — which is why 1+2 go first (your last note).


## 8. Bot answers DMs per brand guidelines

Which DMs? Two very different products:
- **Telegram chat with the bot answering brand questions** (owner asks "what's our return
  policy?") — trivial, one LLM call with `brand_text`, but low value.
- **Instagram DMs / comments from customers** — a customer-facing product: Instagram
  Messaging API, `instagram_manage_messages` + `instagram_manage_comments` scopes (new app
  review), webhook subscriptions, real-time expectations, and reputational risk when the
  bot is wrong. If this is the one: **keep the human in the loop** — the bot *drafts* a
  reply, DMs it to the owner with ✅ Send / ✏️ Fix / ❌, exactly the post loop. Comments
  first (public, low volume, brand-building), DMs second. It generalises `Post` into a
  "reviewable item" — a real refactor, so it belongs last, after item 4's seam.

**Suggestion:** clarify which; if Instagram, schedule it after 7 and run the Meta review
early (weeks of lead time, like M7).

**feedback** - instagram, reply instagram dms, have a guidline from the brand and in case a human response is needed, the but will botify via telegram and/or whatsapp - start with telegram, whatsapp if feedback requires it.
**→ Clear. Facts that shape the design (Meta rules, verify in the dev docs):**
- Replies are allowed only **within 24h of the user's last message**; sends outside fail (error 10/2534022). The **Human Agent tag** extends to 7 days but is *for humans only* — Meta detects bot use; sending your Telegram-typed reply through it is defensible, an LLM draft is not.
- A comment grants **one private DM within 7 days**; if they answer, the 24h window opens — comments are the cheap on-ramp.
- Needs `instagram_manage_messages` (newer docs: `instagram_business_manage_messages`) with **Advanced Access app review**, webhook subscriptions, and a **disclosure** that the user may be talking to an assistant.
Design, three tiers, all driven by a `## DM guidelines` section in the brand text:
1. **Auto-answer** when the guideline covers it with high confidence (hours, prices, shipping, returns) — the model must cite the guideline line it used; no citation → no auto-answer.
2. **Draft to Telegram** for everything else: the thread + a drafted reply + ✅ Send / ✏️ Fix / ❌ Ignore, with the **24h countdown shown on the card**. Same loop as posts — `Post` becomes the general "reviewable item", which is the real refactor here.
3. **Escalate**: "needs a human" (complaints, anything about money, anger) → Telegram with the full thread; you reply in Telegram and the bot relays. Every ✏️ Fix on a reply is a learning signal for the DM guideline, like posts.
WhatsApp as the owner channel later, as you say. Plan updated: Meta review starts during onboarding (M13) because it's weeks of lead time.


## 9. Carousels

Already planned (ROADMAP M9). Fine at the bottom — but it's a self-contained 2–3 day item
with the only blocker being the `## Carousels` brand-file section, so it works as filler
between bigger milestones whenever a week has slack.

---

## Proposed order

| # | Milestone | Covers | What ships | Size |
|---|-----------|--------|------------|------|
| M9 | **Bot console v2** | 1, 2a, 2b, 2c | reply keyboard + HTML, `/schedule`, `/mode learning` + `📚 Keep`, `🤏 Almost` + `🔁 Retry`, near-miss learning block | 1 wk |
| M10 | **Tenant seam** | 4a | `brands` table (one row), `brand_id` everywhere, brand text into DB, `cost_usd` | 2–3 d |
| M11 | **Media v2** | 3 | Telegram files + albums, `/stock` paging + thumbs, **Google Drive folder sync with status write-back**; Photos Picker `/import` optional | 1 wk |
| M12 | **Anchors (steering)** | 6a, 6b-steer | `/idea`, opt-in calendar prompts, brand-facts stock + cooldowns + ratio ceiling, anchor-steered image pick | 1 wk |
| M13 | **Beta onboarding** | 4b (lite) | `/start` wizard, per-brand IG connect + token refresh, learning mode by default; **start Meta review for messaging** | 1–2 wk |
| M14 | **Instagram DM assistant** | 8 | auto / draft-to-Telegram / escalate tiers, 24h-window aware, `Post` → reviewable item | 2 wk |
| M15 | **Analytics loop** | 7 | insights job, top-performers in learning, weekly digest — when there's data | 3 d |
| M16 | **Limits & billing** | 4b (rest) | per-brand usage caps, cost in `/status`, invoicing; payment provider only on demand | 3–4 d |
| — | **Carousels** | 9 | filler between any two of the above | 2–3 d |
| backlog | **X** (pay-per-use ≈ $0.015/post), text-card renderer, media-less generation | 5, 6b-render | when a brand asks for a text network | — |

Two things I'd push back on from your order: **multi-brand at #4 before anchors and
analytics** means onboarding brands into a product that still only works with a full
stock library and can't show them results — M12/M13 make the offer better before M14
asks anyone to try it. And **X at #5** is the most expensive network for the least
Israeli reach; Facebook Page gets a second network for a tenth of the effort.

**feedback** - the starting brands will be beta testers for the product so they dont need a pitch
**→ Agreed — the "analytics before onboarding" argument is void for beta testers. Reordered: onboarding moves up, analytics moves to the end. The tenant seam still goes early, but after items 1+2 (your next note).**


Open owner steps that are still pending and don't move: the M8 learning A/B (paid) and
a real-chat round-trip of ✏️ Fix and `/distill` on prod.
**feedback** - i want to implement item 1 and 2 before doing this because it will make it easier for me
**→ Agreed: items 1 + 2 first, as M9.** The A/B and the Fix/distill round-trip wait until the console makes them easy. The tenant seam slides to M10 — the only cost is that M9's new `settings` table gets a `brand_id` column in M10's migration instead of from day one (trivial). Final order below replaces the table above.

---

Sources checked 2026-10-14 for the facts above (verify on the vendors' own pages before building):
- X pricing: [X API pricing 2026 — pay-per-use](https://www.outstand.so/blog/x-api-pricing), [tiers explained](https://www.wearefounders.uk/the-x-api-price-hike-a-blow-to-indie-hackers/), [what posting costs](https://invisibleapi.ai/blog/x-api-pricing/)
- Google Photos: [Updates to the Google Photos APIs (official)](https://developers.google.com/photos/support/updates), [Picker API launch blog](https://developers.googleblog.com/en/google-photos-picker-api-launch-and-library-api-updates/), [gphotos-sync issue #511](https://github.com/gilesknap/gphotos-sync/issues/511)
- Instagram messaging: [DM automation rules 2026](https://www.spurnow.com/en/blogs/instagram-dm-automation-rules), [24-hour window](https://sumgenius.ai/blog/instagram-24-hour-messaging-window-2026/), [messaging approval guide](https://singhamandeep.com/instagram-messaging-api-approval-getting-instagram_business_manage_messages-2026/), [documented limits](https://www.conferbot.com/limits/instagram)

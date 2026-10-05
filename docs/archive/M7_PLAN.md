# M7 — First real publisher: Instagram (single-image feed posts)

## Context

M0–M6 shipped: the full generate→review→queue→publish loop runs autonomously in prod
with **stub** publishers (`app/publishers/base.py` logs instead of posting). M7 replaces
one stub — Instagram — with a real [Instagram Graph API][ig] adapter, **without touching
pipeline code**. The `Publisher` protocol and the `_do_publish` broadcast loop
(`app/pipeline/publish.py`) stay as the seam; only the adapter and its supporting surface
change.

**Locked decisions (2026-07-14):**
- **Single-image feed posts only.** The pipeline is single-image end to end (`stock`
  picks one image per post, vision captions one image, the review DM is one `sendPhoto`).
  Carousel / multi-image is **deferred to the backlog** — it needs a `post-images` table
  plus generation + review rework and is out of scope here.
- **Serve the image from the app.** Graph fetches the image from a public HTTPS URL (you
  cannot upload image bytes for a feed post), so CuteBot exposes `GET /media/{image_ref}`
  on its existing public Railway domain. No external storage / CDN.
- **Instagram only is "real" in M7.** TikTok and X stay logging stubs; `get_publishers()`
  returns the real Instagram adapter plus the two stubs (or Instagram alone — see M7.4).

[ig]: https://developers.facebook.com/docs/instagram-platform/content-publishing

### The Graph publishing flow (why the surface grows)

Publishing one feed image is **two Graph calls plus a poll**, all keyed by a public image
URL and a long-lived token:

1. **Create container** — `POST /{version}/{ig-user-id}/media` with `image_url`,
   `caption`, `access_token` → returns `{"id": "<container-id>"}`.
2. **Poll status** — `GET /{version}/{container-id}?fields=status_code` until
   `status_code == "FINISHED"` (values: `IN_PROGRESS`, `FINISHED`, `ERROR`, `EXPIRED`,
   `PUBLISHED`). Containers process asynchronously.
3. **Publish** — `POST /{version}/{ig-user-id}/media_publish` with
   `creation_id=<container-id>`, `access_token` → returns `{"id": "<media-id>"}`. Live.

This forces three things the stubs never needed: a public image URL (M7.2), real auth +
config (M7.4), and **crash-safe idempotency** so recovery can't re-post to a real network
(M7.3 — the load-bearing ticket).

## Approach

### M7.1 — Instagram Graph adapter — `app/publishers/instagram.py` (new)

Replace `InstagramPublisher(_LoggingStubPublisher)` in `base.py` with a real adapter in
its own module. Implements the same `Publisher` protocol (`name = "instagram"`,
`async def publish(...) -> PublishResult`). Uses the shared `httpx` (already a dep).

- Build the caption with `render_full_caption(post, settings)` (he + en + disclaimer —
  well under IG's 2200-char / 30-hashtag limits).
- Build the image URL as `{public_base_url}/media/{post.image_ref}` (M7.2 serves it).
- **Validate before container creation** (log + fail the `PublishResult`, don't crash the
  loop): IG feed images must be JPEG, width 320–1440 px, aspect ratio 4:5–1.91:1, ≤ 8 MB.
  Out-of-spec → `PublishResult(ok=False, detail=...)` so the post goes `FAILED` (owner can
  requeue after fixing stock) rather than a confusing Graph error.
- Container create → poll `status_code` (bounded: N tries with a short sleep, timeout →
  `ok=False`) → `media_publish`. Map Graph error bodies (`error.message`,
  `error.code`) into `PublishResult.detail`.
- Token, IG user id, and Graph version come from config (M7.4). No secrets logged.

**Idempotency lives here, coordinated with M7.3:** the adapter persists progress markers
(`ig_container_id` after create, `ig_media_id` after publish) so a crash-restart can tell
what already happened. This needs DB access from the adapter — see M7.3 for how the
session is threaded.

### M7.2 — Public media route — `app/main.py` + `app/render.py`

Graph must fetch the image over HTTPS, so serve it:

- `GET /media/{image_ref:path}` → read `render.image_path(post_ref)` from the stock dir,
  return the bytes. **Not** dev-gated (Graph is an anonymous fetcher). Guard against path
  traversal: resolve under `stock_images_dir` and 404 anything that escapes it.
- **JPEG conversion:** IG feed accepts JPEG only; stock may hold png/webp. Convert to JPEG
  on the fly with **Pillow** (new dep — add to `requirements.txt` *and* `pyproject.toml`).
  Set `Content-Type: image/jpeg`.
  - *ponytail alternative if you want to skip Pillow:* require stock to be JPEG and serve
    bytes as-is. Rejected here because png/webp stock would silently fail at publish time;
    Pillow is small and removes a foot-gun.
- New config `public_base_url` (M7.4) is the origin for the URL the adapter hands Graph.

### M7.3 — Crash-safe real publish — migration `0004` + recovery rework

**The danger the stubs never had:** stubs are idempotent; Instagram is not. Today
`recover_orphaned` (`publish.py:25`) blindly resets every `PUBLISHING` post to `APPROVED`
on startup. With a real publisher, a crash *after* `media_publish` succeeds but *before*
CuteBot commits `PUBLISHED` would, on restart, **re-post to Instagram**. Must not happen.

- **Schema (migration `0004`, autogenerate + review):** add nullable
  `Post.ig_container_id: str | None` and `Post.ig_media_id: str | None`. (Generic enough
  that a future TikTok/X adapter can reuse the pattern; keep IG-specific names for now —
  YAGNI on a shared `external_ids` table until a second real network exists.)
- **Adapter writes markers mid-flight** (M7.1): commit `ig_container_id` *before*
  `media_publish`; commit `ig_media_id` *after*. This requires the adapter to persist to
  the DB, so **thread the session into the publish path**: change the `Publisher` protocol
  to `async def publish(self, session: AsyncSession, post: Post) -> PublishResult` and
  update `_do_publish` to pass its session. Stub publishers ignore the arg. This is an
  honest interface evolution (real stateful publishers need to record external ids), not a
  special-case — keep it behind the protocol.
- **Recovery becomes network-aware** — rewrite `recover_orphaned` for `PUBLISHING` posts:
  - `ig_media_id` set → it *did* publish; mark `PUBLISHED` (never re-post).
  - `ig_container_id` set, no `ig_media_id` → query the container `status_code`:
    `PUBLISHED` → mark `PUBLISHED`; `FINISHED`/`IN_PROGRESS` → ambiguous whether
    `media_publish` fired, so **do not auto-republish** — set `FAILED` and DM the owner to
    resolve (owner checks the IG account, then requeues or lets it lie). `ERROR`/`EXPIRED`
    → safe to reset to `APPROVED` (nothing was published).
  - Neither id set → Graph was never called; safe to reset to `APPROVED` (current
    behaviour).
  - Posts with no real-network markers (pure-stub era) → `APPROVED` as today.

  *ponytail:* single-worker in-process scheduler, so no concurrent publishers race the
  same post; this recovery check at startup is sufficient. Add per-post leases only if v1
  ever goes multi-worker (same ceiling already noted in `publish.py`).

### M7.4 — Config + activation — `app/config.py` + `env.example`

Add settings (Instagram token already exists as `instagram_access_token`):
- `instagram_ig_user_id: str = ""` — the IG Business account id.
- `instagram_graph_version: str = "v21.0"` — pinned Graph API version.
- `public_base_url: str = ""` — origin CuteBot serves `/media/...` from (e.g.
  `https://cutebot-production.up.railway.app`). Note the overlap with
  `telegram_webhook_base`; keep it a distinct setting so media hosting and the webhook can
  differ, but document that in prod they're usually the same value.

Activation in `get_publishers()`: return the real `InstagramPublisher` when
`instagram_access_token` **and** `instagram_ig_user_id` are set, else fall back to the
Instagram stub (so dev/test without creds still exercises the loop). TikTok/X stay stubs.
Mirror all new vars in `env.example` with comments; no secrets committed.

### M7.5 — Tests — `tests/`

- **Offline (default suite):** monkeypatch the Graph HTTP calls (patch the adapter's
  httpx client / a small `_graph_post`/`_graph_get` seam) — assert: caption + image_url
  composition; the two-step create→poll→publish order; a `PublishResult(ok=True)` on the
  happy path and `ok=False` (post → `FAILED`) on a Graph error.
- **Idempotency test (the important one):** simulate a crash between `media_publish` and
  the `PUBLISHED` commit (post left `PUBLISHING` with `ig_media_id` set), run
  `recover_orphaned`, assert the post ends `PUBLISHED` and **no second `media_publish`
  call is made**. Also: container created but not published (`ig_container_id` only,
  status `IN_PROGRESS`) → post ends `FAILED` + owner notified, no re-post.
- **Opt-in live contract test** (deselected by default, like the M1 LLM contract test):
  gated on `INSTAGRAM_ACCESS_TOKEN` + `INSTAGRAM_IG_USER_ID` env; publishes one real post
  to a test IG account and asserts a `media-id` comes back.

### M7.6 — Meta paperwork + deploy runbook — `docs/`

The long external-lead-time item — **start the paperwork immediately, in parallel with
the code:**
- **Meta app setup:** create/confirm a Meta app, add the Instagram Graph product, link the
  IG **Business/Creator** account to a Facebook Page.
- **Permissions + review:** `instagram_basic`, `instagram_content_publish`,
  `pages_show_list`, `pages_read_engagement` — these require **Business Verification** and
  **App Review** (the weeks-long step). Document the exact scopes and the review
  submission notes.
- **Token lifecycle:** generate a long-lived token (~60 days), document the manual refresh
  procedure and where it lives in Railway env. (Automated refresh is a follow-up, not M7.)
- **Deploy runbook + smoke:** env inventory, then one real post to a **test IG account**
  from prod, verified live on the account. Only after that, point at the real account.

## Files

- **Add:** `app/publishers/instagram.py`, `alembic/versions/0004_instagram_external_ids.py`,
  `docs/M7_INSTAGRAM_RUNBOOK.md` (or fold into `SCRIPTS_REFERENCE.md`), tests in
  `tests/`.
- **Modify:** `app/publishers/base.py` (drop the IG stub, `Publisher.publish` gains
  `session`, `get_publishers()` activation), `app/pipeline/publish.py` (pass session to
  `publish`; network-aware `recover_orphaned`), `app/models.py`
  (`ig_container_id`/`ig_media_id`), `app/config.py` + `env.example` (new vars),
  `app/main.py` (`GET /media/...`), `app/render.py` (path-safe resolve for the route),
  `requirements.txt` + `pyproject.toml` (Pillow), `SCRIPTS_REFERENCE.md`, `ROADMAP.md`
  (M7 shipped).

## Verification

1. `ruff check . && mypy app && pytest` — all green; suite stays offline (Graph mocked).
2. `alembic upgrade head` on scratch SQLite applies `0004`; `alembic check` reports no
   drift vs models.
3. `GET /media/{ref}` returns a valid JPEG for a png/webp stock image (Pillow conversion),
   404s on a traversal attempt (`../../etc/passwd`).
4. Idempotency: the crash-simulation test proves recovery never issues a second
   `media_publish`.
5. **Live (opt-in, test IG account):** with real creds, a queued approved post publishes
   to Instagram at a slot; the account shows the image + bilingual caption + disclaimer;
   `posts` row goes `approved → publishing → published` with `ig_media_id` set. Kill the
   process mid-publish → restart → no double-post.

## Out of scope (later)

- **Carousel / multi-image** → backlog (needs a `post-images` table + generation/review
  rework).
- **TikTok and X real adapters** — stay stubs (roadmap §E remainder).
- **Automated token refresh** — M7 documents manual refresh; automate later.
- **Real account cutover** — M7 proves the path against a test IG account; switching to
  the production account is an ops step, not code.

# M0 — Image-first, bilingual, disclaimer-bearing generation

> Implementation plan for milestone **M0** in `ROADMAP.md`. Approved; to be executed in a
> later session. `PRODUCT_SPEC.md` is the source of truth — if they ever disagree, the
> spec wins and this doc is corrected.

## Context

CuteBot's skeleton runs the full generate→review→queue→publish loop (green: ruff/mypy/
8 tests/CI), but three product rules added to `PRODUCT_SPEC.md` are not yet in the code:

1. **Image-first** generation off an owner-provided **stock library** (no image gen) —
   pick an image, then caption it with **vision** (Claude sees the actual image).
2. Every post is **bilingual, Hebrew-first** — stored as separate `caption_he` /
   `caption_en` fields; Hebrew quality is a hard gate.
3. Every post carries a **cute CuteBot disclaimer**, appended **in code** from a
   configurable template so the guarantee can't regress.

Today generation is text-only and batched (`llm.generate_suggestions(brand, n)`), `Post`
has a single `caption`, Telegram sends `sendMessage`, and publishers are logging stubs.
M0 closes this gap end-to-end and is the critical path for the whole roadmap (`ROADMAP.md`)
— everything downstream assumes an image-bound, bilingual post.

**Locked decisions:** vision captioning; v1 = prove-the-loop (stub publishers); Alembic
introduced now with a baseline migration; disclaimer default `🤖 מאת CuteBot · by CuteBot`.

## Approach

### 1. Data model — `app/models.py`
On `Post`: **replace** `caption` with `image_ref`, `caption_he`, `caption_en` (all
`Mapped[str] = mapped_column(Text)`); keep `visual_concept`, `rationale`, status, etc.
`image_ref` stores the image path **relative to** `STOCK_IMAGES_DIR` (portable). The
disclaimer is **not** a column — it's composed at render/publish from config.

### 2. Config — `app/config.py` + `env.example`
Add settings (with the parsing-property pattern already used by `posting_slots_list`):
- `stock_images_dir: str = "stock"`
- `primary_language: str = "he"`
- `secondary_languages: str = "en"` + a `secondary_languages_list` property
- `post_disclaimer: str = "🤖 מאת CuteBot · by CuteBot"`

Mirror all four in `env.example`. `stock/` holds real owner assets → add to `.gitignore`
(like `brand.yaml`); do **not** commit images. Document "drop images into `stock/`" in
`env.example` + README quickstart.

### 3. Stock library — `app/stock.py` (new)
- `list_images(settings) -> list[Path]` — enumerate `*.jpg/.jpeg/.png/.webp` under
  `STOCK_IMAGES_DIR`.
- `select_images(session, n, settings) -> list[Path]` — prefer images whose relative
  path is **not** already in any `Post.image_ref` (rotation); if the unused pool is
  smaller than `n`, top up by cycling already-used ones. Empty dir → return `[]` and log
  a clear warning (generation produces nothing rather than crashing).
- `load_image_b64(path) -> tuple[str, str]` — `(mime_type, base64)` for the vision call.

### 4. Caption composition — `app/render.py` (new; supersedes the roadmap's `disclaimer.py`)
Single source of the "bilingual + disclaimer always present" guarantee, used by **both**
the notifier and publishers:
- `render_full_caption(post, settings) -> str` — primary-language caption first, then each
  secondary, then a blank line + `settings.post_disclaimer`. Ordered by
  `primary_language` (he → en by default).
- `image_path(post, settings) -> Path` — resolve `STOCK_IMAGES_DIR / post.image_ref`.

### 5. LLM seam — `app/llm.py` (multimodal, bilingual, **per-image**)
Replace `generate_suggestions(brand, n)` with `caption_image(brand, image_path, settings)
-> PostSuggestion` — **one vision call per image** (reliable image↔caption mapping and
better captions than one batched multi-image call; batch sizes are small).
- Build OpenAI/LiteLLM-style multimodal content: a text block (brand + language
  instructions + JSON shape) **plus** an `image_url` block with a
  `data:{mime};base64,{b64}` URL. Keep `response_format={"type":"json_object"}` and the
  explicit `api_key=_provider_key()` (LiteLLM reads keys from `os.environ` only — keep
  this; see `app/llm.py` current comment).
- Prompt demands **native-quality Hebrew (primary) + English**, caption grounded in the
  image, brand hard-rules respected, and **must not** add the CuteBot disclaimer (code
  owns it). Returns JSON `{caption_he, caption_en, visual_concept, rationale}`.
- Offline stub (`_stub_suggestions`) returns a `PostSuggestion` with both language fields
  filled, referencing the image filename, so the suite stays fully offline.
- `GenerationResult` (the list wrapper) is no longer needed — validate the single object
  into `PostSuggestion` directly; remove it.

### 6. Schemas — `app/schemas.py`
- `PostSuggestion`: `caption_he`, `caption_en`, `visual_concept`, `rationale` (drop
  `caption`).
- `PostOut`: expose `image_ref`, `caption_he`, `caption_en` (drop `caption`).

### 7. Generate stage — `app/pipeline/generate.py`
`images = stock.select_images(session, size, settings)`; for each image
`s = await llm.caption_image(brand, image, settings)` → create `Post(image_ref=<relative
path>, caption_he=s.caption_he, caption_en=s.caption_en, visual_concept=…, rationale=…,
status=SUGGESTED, batch_id=batch.id)`. `batch.size = len(images)`. Same commit/refresh
flow as today.

### 8. Telegram notifier — `app/notifier/telegram.py`
`send_suggestion` switches to **`sendPhoto`** (httpx multipart `files={"photo": …}`) with
`caption = render_full_caption(post, settings)` and the existing Approve/Reject inline
keyboard (callback data `approve:/reject:<id>` is unchanged). Telegram photo captions cap
at 1024 chars: if the rendered caption exceeds it, send the photo without buttons, then a
`sendMessage` carrying the full text **and** the keyboard (so controls always sit with the
full caption). Photo bytes come from `render.image_path(post, settings)`.

### 9. Publishers — `app/publishers/base.py`
`_LoggingStubPublisher.publish` composes `render_full_caption(post, get_settings())` and
logs it **with `image_ref`**, so stubs show the real bilingual+disclaimer+image payload.
Protocol and `publish.py` are unchanged (still `publish(post) -> PublishResult`).

### 10. Alembic (new) — baseline migration
- Add `alembic==1.14.x` to `requirements.txt` (pyproject defers runtime deps to it).
- `alembic init alembic`; rewrite `alembic/env.py` for **async** (engine from
  `app.db.make_engine`, `target_metadata = app.db.Base.metadata`, URL from
  `get_settings().database_url`, offline+online via `connection.run_sync`). Import
  `app.models` so metadata is populated.
- One **baseline** `versions/0001_baseline.py` creating `batches`, `posts` (with
  `image_ref`/`caption_he`/`caption_en`, no `caption`), `feedback`. `init_db()`/
  `create_all` stays for dev/test; M4 runs `alembic upgrade head` on Postgres.
- `SCRIPTS_REFERENCE.md`: add `alembic revision --autogenerate -m …` and
  `alembic upgrade head` under a Database section.

### 11. Tests — `tests/`
- Rewrite the autouse `_stub_llm` fixture to patch **`llm.caption_image`** returning a
  `PostSuggestion(caption_he=…, caption_en=…, …)`; monkeypatch `stock.select_images` to
  return fake `Path`s so no real files/network are needed.
- Update existing generate/queue/publish/idempotency tests to the new `PostSuggestion`
  shape and `image_ref`.
- **New assertions:** every generated post has non-empty `image_ref`, `caption_he`,
  `caption_en`; `render_full_caption` contains both captions **and** the disclaimer for a
  sample post.

## Files

- Modify: `app/models.py`, `app/config.py`, `app/schemas.py`, `app/llm.py`,
  `app/pipeline/generate.py`, `app/notifier/telegram.py`, `app/publishers/base.py`,
  `env.example`, `.gitignore`, `requirements.txt`, `README.md`, `SCRIPTS_REFERENCE.md`,
  `tests/test_pipeline.py` (+ `tests/conftest.py` if a stock fixture is cleaner).
- Add: `app/stock.py`, `app/render.py`, `alembic.ini`, `alembic/env.py`,
  `alembic/versions/0001_baseline.py`.

## Verification

1. `ruff check . && mypy app && pytest` — all green (suite stays offline).
2. `alembic upgrade head` against a scratch SQLite URL creates the schema; `alembic
   check` (or autogenerate) reports no drift vs the models.
3. Drop 2–3 images into `stock/`, set `ANTHROPIC_API_KEY`, run the app on `127.0.0.1:8002`
   and `POST /dev/generate`: posts persist with a real `image_ref` + Hebrew & English
   captions; logs show the stub-publisher payload including the disclaimer.
4. With `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` + `python -m app.notifier.telegram poll`:
   the DM is a **photo** with the Hebrew+English caption, the disclaimer, and working
   Approve/Reject buttons; a decision writes a `Feedback` row.
5. `POST /dev/publish-next` drains the front of the queue once (idempotent), logging the
   composed bilingual caption + image for each stub network.

## Out of scope (later milestones)
Live `ANTHROPIC_API_KEY` Hebrew-quality tuning + retries/cost guards (M1); real Telegram
round-trip hardening / message-edit-on-decision (M2); Postgres + `alembic upgrade` on
Railway (M4); real network publishers (post-v1).

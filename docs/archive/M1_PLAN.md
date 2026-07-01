# M1 — Real generation, validated

> Implementation plan for milestone **M1** in `ROADMAP.md`. To be executed in a later
> session. `PRODUCT_SPEC.md` is the source of truth — if they disagree, the spec wins and
> this doc is corrected. Predecessor: `M0_PLAN.md` (shipped, commit `ca20caa`).

## Context

M0 made generation **image-first, bilingual (Hebrew-first), disclaimer-bearing**, and it
runs fully offline via a deterministic stub. What M0 *never did*: hit a real model. Every
test patches `llm.caption_image`; the live multimodal path (`litellm.acompletion` with a
`data:` image URL) has not executed once.

M1 is the **first contact with reality**: run against a live `ANTHROPIC_API_KEY`, confirm
the multimodal request shape and JSON contract actually hold, get the **Hebrew quality** to
native level, and make the LLM seam survive the failures real APIs produce (timeouts, rate
limits, malformed JSON). M1 ships no new product surface — it makes the M0 surface
*trustworthy*.

**Grounding in the current code (not the pre-M0 plan):**

- `app/llm.py:106` — a **single** `acompletion`, `max_tokens=2000` hardcoded, **no
  timeout, no retries**. Any transient error propagates raw.
- `app/llm.py:116` — `json.loads` + `model_validate` with **no guard**: a non-JSON or
  missing-field response raises `JSONDecodeError`/`ValidationError` straight up the stack.
- `app/pipeline/generate.py:60` — `commit()` is **after** the per-image loop, so one
  `caption_image` exception on image 3/5 **discards the entire batch** (and the `Batch`
  row). This is the real "graceful failure" gap.
- `app/stock.py:33` — rotation/dedup **already shipped** in M0. M1 only *verifies* it
  cycles; no new rotation code.
- `brand.example.yaml` exists; there is no realistic `brand.yaml` driving prompt tuning.
- `app/config.py:24` — default model is `anthropic/claude-sonnet-4-6` (cost-effective for
  volume per CLAUDE.md). M1 evaluates whether the Hebrew gate needs `claude-opus-4-8`.

**Locked decisions:** keep the offline stub + offline test suite (CI stays keyless); use
LiteLLM's **built-in** `timeout`/`num_retries` rather than a hand-rolled retry loop; a
per-image failure degrades to a **smaller batch**, never a lost batch.

## Approach

### 1. Harden the LLM seam — `app/llm.py`
Make `caption_image` survive real-API failure without hand-rolling control flow:

- Pass `timeout` and `num_retries` **into `acompletion`** (LiteLLM handles backoff on
  429/5xx/timeout natively — do not write a retry loop). Source both from config.
- Wrap **JSON parse + validation** (`json.loads` → `PostSuggestion.model_validate`) in a
  `try/except (json.JSONDecodeError, ValidationError)`; on failure, log the offending
  content (truncated, no secrets) and raise a single typed `CaptionError` so the caller
  can decide batch policy. Don't silently stub — a configured key that returns garbage is
  a real error the reviewer must see.
- Keep `max_tokens` but source it from config (`llm_max_tokens`) instead of the literal
  `2000`.
- Leave the offline-stub branch untouched (it's the keyless contract).

### 2. Config — `app/config.py` + `env.example`
Add, with sensible defaults (no new behavior when unset):
- `llm_timeout_s: int = 60`
- `llm_num_retries: int = 2`
- `llm_max_tokens: int = 2000`

Mirror in `env.example` with one-line comments. No new property parsing needed.

### 3. Resilient batch generation — `app/pipeline/generate.py`
One image failing must not lose the others:
- `try/except CaptionError` **around the per-image `caption_image` call** inside the loop;
  on failure, log `image_ref` + reason and `continue` (skip that image).
- Set `batch.size = len(posts)` **after** the loop (actual successes), not
  `len(images)` up front.
- If **every** image fails, commit the empty `Batch` (provenance of the failed run) and
  return `[]`; the scheduler logs "0 suggestions" rather than crashing the job.
- Keep the single post-loop `commit()` — partial success commits the posts that worked.

### 4. Hebrew quality gate (process, not code) — the heart of M1
This is review-and-tune, iterated against a **live key**:
- Write a realistic `brand.yaml` (a real-ish brand, concrete voice/do-not/pillars) so the
  prompt is exercised under representative input. Keep `brand.example.yaml` as the template.
- Run `POST /dev/generate` against 5–10 real stock images with `ANTHROPIC_API_KEY` set.
- **Native Hebrew read** of the output: caption must read as *written-in-Hebrew*, not
  translated — correct gender/number agreement, natural idiom, no calque from English, RTL
  punctuation sane. The English must match the Hebrew in *voice*, not be a literal echo.
- Tune `_SYSTEM_PROMPT` / `_USER_TEMPLATE` in `app/llm.py` until both hold. Likely levers:
  explicit "write Hebrew first, then render the English to match — do not translate",
  brand-voice emphasis, and image-grounding strictness.
- **Model decision:** if Sonnet's Hebrew doesn't clear the gate after prompt tuning, bump
  the default (or a dedicated `generation` model env) to `claude-opus-4-8` and record the
  cost/quality trade in `DEV_GUIDELINES.md`. Decide with evidence, not upfront.

### 5. Verify the multimodal contract live — one real assertion
The offline suite can't prove the wire shape. Add a **single, opt-in** live test
(`tests/test_llm_live.py`, skipped unless `ANTHROPIC_API_KEY` is set) that captions one
real small image and asserts: non-empty `caption_he` containing Hebrew characters
(`֐-׿`), non-empty `caption_en`, and `visual_concept`/`rationale` present. This
is the one check that the `data:` URL + `json_object` response_format actually work
end-to-end. Marked `@pytest.mark.live`; CI stays keyless and skips it.

### 6. Confirm rotation cycles (verify, don't build)
Rotation shipped in M0 (`stock.select_images`). M1 just confirms behavior with a small
unit test: with 3 stock images and 2 already referenced by `Post.image_ref`, two
successive `select_images(n=2)` calls exhaust the unused image first, then cycle — no
unused image repeats while an unused one remains. (If the existing M0 tests already cover
this, skip — don't duplicate.)

## Files

- Modify: `app/llm.py` (timeout/retries/parse-guard/`CaptionError`/config max_tokens),
  `app/config.py` + `env.example` (3 settings), `app/pipeline/generate.py` (per-image
  try/except, real `batch.size`), `DEV_GUIDELINES.md` (Hebrew-gate findings + model
  decision), `SCRIPTS_REFERENCE.md` (note the live test marker if added).
- Add: `brand.yaml` (gitignored — real brand; **do not commit**), `tests/test_llm_live.py`
  (opt-in live test), optionally one rotation unit test in `tests/test_pipeline.py`.
- No DB shape change ⇒ **no migration** this milestone.

## Verification

1. `ruff check . && mypy app && pytest` — green, fully offline (live test skipped).
2. **Resilience:** with a key set, monkeypatch `caption_image` to raise on the 2nd of 3
   images; `generate_batch` returns 2 posts, `batch.size == 2`, and the run logs the
   skipped image. With *all* raising, returns `[]` and does not crash.
3. **Live contract:** `ANTHROPIC_API_KEY` set + `pytest -m live` → the one live test passes
   (real Hebrew + English from a real image).
4. **Hebrew gate (human):** `POST /dev/generate` over a real batch; a Hebrew speaker
   confirms native-quality captions on-brand, disclaimer present, English matched in voice.
5. **Degrade-gracefully:** unset the key → still returns stub posts (M0 contract intact);
   set a key but force a malformed response → `CaptionError` logged, batch keeps the good
   posts.

## Out of scope (later milestones)
Telegram round-trip hardening / edit-on-decision (M2); end-to-end slot draining (M3);
Postgres + `alembic upgrade` on Railway (M4); real network publishers and learning-loop
few-shot from `Feedback` (post-v1). No per-run cost *budget* enforcement yet — `max_tokens`
+ retry caps are enough for v1; add a budget guard only if a real bill says so.

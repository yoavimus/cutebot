# Development Guidelines

**Remember**: check these guidelines during development; update them as the project
teaches you things.

## Best practices

1. **Test before moving on** — `ruff check . && mypy app && pytest`. Verify terminal
   output for errors before continuing.
2. **The approval gate is sacred** — publishing may only ever act on posts in
   `approved` status. Treat any code path that could publish a `suggested`/`rejected`
   post as a bug.
3. **Keep it simple** — no Redis, no worker, no multi-tenant in v1. Resist
   over-engineering; the scheduler runs in-process.
4. **Commit often** — small, focused commits. Ask before pushing to origin.
5. **Avoid patchwork** — don't add quick fixes that break when the pipeline grows;
   extend via the Notifier/Publisher interfaces, don't special-case a network.
6. **Learn** — capture non-obvious findings in memory (project or global) and update
   this file when a guideline emerges from experience.
7. **Refactor responsibly** — DB shape change ⇒ add a migration; new library ⇒ update
   `requirements.txt` **and** `pyproject.toml`; new script ⇒ update `SCRIPTS_REFERENCE.md`.

## Architecture principles

### Structured data over parsing
The LLM returns a validated Pydantic object (`PostSuggestion`), never free text that
later gets parsed. If you need a new field, add it to the schema and the prompt.

### Interfaces, not branches
New review channel (Discord/Slack) ⇒ new `Notifier` adapter. New network
(Instagram/TikTok/X) ⇒ new `Publisher` adapter. Pipeline code depends on the
interface, never on a concrete adapter.

### One LLM seam
All model access goes through `app/llm.py`. Never import `litellm` or an SDK from
pipeline modules. Model choice is config (`DEFAULT_LLM_MODEL`), not code.

### Idempotent publishing
Move a post to `publishing` **before** any network call, so a crash or retry can't
double-post. Only `ok` → `published`; failure → `failed` (re-queueable later).

## AI / Claude usage

- Runtime model: via LiteLLM (`DEFAULT_LLM_MODEL`, default `anthropic/claude-opus-5-5` — see
  "Model decision" below). For LLM/API questions, read the `claude-api` skill — don't
  answer model questions from memory.
- Keep prompts in `app/llm.py` (or a `prompts/` dir if they grow). Brand guidelines
  are injected verbatim from the brand row (`brands.text(brand)`, the single read); the
  learning context from `app/learning.py` is appended after them.
- **Brand text lives in the DB (M10); `brand.md` is only the seed.** Boot copies `BRAND_FILE`
  into row 1 when the text is empty; after that editing the file does nothing until
  `scripts/brand_sync.py push`. `app/distill.py` writes the row after an explicit ✅ in
  Telegram and keeps the old text in `brand_text_prev` (no more `.bak`). Code never writes
  `brand.md` except `brand_sync.py pull`.

## Code quality

- Run the app after changes; confirm the scheduler logs its registered jobs.
- Exercise the Approve/Reject round-trip against a real test chat before shipping
  review-loop changes.
- After editing nested async code, re-run mypy — async SQLAlchemy typing bites.
- No secrets in code or logs. Scrub tokens from any debug output.

## Bot console (M9) lessons

- **Enforce learning mode at the doors.** `review.handle_decision` rewrites approve→keep and
  `publish_next` / `publish_by_id` return early; pausing slot jobs alone doesn't stop
  catch-up, `/postnow`, `/dev` endpoints or the scheduler-less polling CLI. Any new publish
  path goes through those or asks `prefs.mode()`.
- **Fail closed on settings.** Unreadable `settings` at boot ⇒ learning mode + a DM, never
  live. `prefs.mode()` raises on DB errors; only `load_schedule` converts that.
- **Env is the default, the DB row wins** — read schedule/mode via `app/prefs.py`, never
  `Settings` directly at runtime.
- **APScheduler 3:** `add_job(next_run_time=None)` adds the job *paused*; omit the kwarg
  otherwise. Re-add slot jobs with the same ids so reschedules replace, not duplicate.
- **Telegram:** `callback_data` ≤ 64 bytes (long refs fall back to the typed command);
  HTML `sendMessage` that is rejected is re-sent as plain text; escape every dynamic value
  with `html.escape`; a chat-scoped `setMyCommands` falls back to the default scope.
- **`send_message(text, *, html, keyboard)`** — notifier fakes in tests must accept the
  kwargs (and `.scheduler`) or console paths die with a `TypeError` that looks unrelated.

## Hebrew quality gate

The LLM must produce **native-quality Hebrew** — written in Hebrew, not translated from English.
Signals of failure: calque phrasing, wrong gender/number agreement, unnatural word order, RTL
punctuation mis-placed.

**Levers (in order):** prompt emphasis ("write Hebrew first, then render English to
match — do not translate") → Hebrew sample posts in `brand.md` → approved examples and
owner edits via the learning context → model choice (below).

**Model decision — process decided 2026-07-08; current pick 2026-10-06: Claude Opus 5.5.**
The choice is made empirically with `scripts/eval_models.py`: same stock images × same
brand file across same-tier candidates from different providers, output reviewed
side-by-side by the owner (native Hebrew speaker). History:
- **Round 1** (2026-10-05; Claude Sonnet 4.6 / Opus 4.8 / GPT-5.1): GPT-5.1 won — but
  the Claude candidates were a generation behind at review time.
- **Round 2–3** (2026-10-05/06; Sonnet 5.5 / Opus 5.5 / GPT-6.1 Sol @medium and @xhigh;
  Astra and Fable excluded on cost, ~$0.05–0.10/post): **Opus 5.5 won on Hebrew quality**
  at ~$0.057/post → runtime default is `anthropic/claude-opus-5-5` (PRODUCT_SPEC §3,
  CLAUDE.md, `app/config.py`, `env.example` updated together — a silent switch is a spec
  deviation). Sonnet 5.5 (~$0.028) is the cost fallback.
- **M8 learning A/B** (Opus 5.5, `--learning` vs not, 10 images): _pending — owner runs it
  against a prod snapshot and records the verdict here._
Standing: re-run after each provider release (candidates take `@effort`), record here.
(Background: docs/archive/POST_V1_REVIEW.md §3.1; owner notes
docs/issues/cutebot_issues_05_10_2026_findings.md §6.)

## RPER

This project follows the RPER loop from global `~/.claude/CLAUDE.md`
(Research → Plan → Execute → Reflect). Persist surprising findings to memory.

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

- Runtime model: Claude via LiteLLM (`DEFAULT_LLM_MODEL`, default
  `anthropic/claude-sonnet-4-6`). For LLM/API questions, read the `claude-api` skill —
  don't answer model questions from memory.
- Keep prompts in `app/llm.py` (or a `prompts/` dir if they grow). Brand guidelines
  are injected verbatim from `brand.yaml`.

## Code quality

- Run the app after changes; confirm the scheduler logs its registered jobs.
- Exercise the Approve/Reject round-trip against a real test chat before shipping
  review-loop changes.
- After editing nested async code, re-run mypy — async SQLAlchemy typing bites.
- No secrets in code or logs. Scrub tokens from any debug output.

## RPER

This project follows the RPER loop from global `~/.claude/CLAUDE.md`
(Research → Plan → Execute → Reflect). Persist surprising findings to memory.

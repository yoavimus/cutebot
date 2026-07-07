# M2 — Review loop hardening

> Implementation plan for milestone **M2** in `ROADMAP.md`. To be executed in a later
> session. `PRODUCT_SPEC.md` is the source of truth. Predecessors: M0, M1 (shipped —
> plans in `docs/archive/`).

## Context

The review loop *works* — `send_suggestion` DMs a photo with Approve/Reject, the webhook
and the poller both route callbacks into `handle_decision`, and decisions are idempotent
(`review.py:38`). M2 makes it feel finished and observable to a human clearing a batch from
their phone:

1. **The message never changes after you tap.** Buttons stay live, nothing shows the
   outcome — you can't tell at a glance what you already decided. Fix: on decision, edit
   the message to mark ✅/❌ and drop the buttons.
2. **The callback answer lies.** Both the webhook (`main.py:71`) and the poller
   (`telegram.py:148`) reply `"Recorded: {decision} #{id}"` **unconditionally** — even for
   an unknown post (`handle_decision` returned `None`) or a double-tap no-op. The toast
   should reflect what actually happened.
3. **Two copies of the callback flow.** `main.py:59-72` and `telegram.py:139-148` each
   parse → decide → answer. They drift (they already differ). Collapse to one function.
4. **Thin logging** on the decision path.

**Grounding in the code:**
- `TelegramNotifier.send_suggestion` (`telegram.py:41`) has two branches: short caption →
  buttons on the **photo**; long caption (>1024) → bare photo + a **separate text message**
  carrying the buttons. Either way, **the button lives on the message the callback fires
  from**, so `cb["message"]` always points at the message to edit.
- `handle_decision` (`review.py:28`) returns the updated `Post`, the unchanged `Post` on a
  double-tap, or `None` for an unknown id — enough to drive an accurate toast.

**Locked decisions:**
- **No DB column, no migration.** The callback query carries `cb["message"]`
  (`message_id`, `chat.id`, and `photo`/`text` so we know which edit method to use). Edit
  *that* message — don't persist a `telegram_message_id`. (If a future need arises to edit
  a message from outside a callback, revisit then — YAGNI now.)
- Keep webhook + poll as the two transports; unify only the **handling** between them.

## Approach

### 1. One shared callback handler — `app/pipeline/review.py` (or `notifier/telegram.py`)
Add `process_callback(session, notifier, cb: dict) -> None` that does the whole flow once:
parse callback data → `handle_decision` → edit the source message → answer the toast with
the **real** outcome. Both `main.py` webhook and `telegram._poll` call it and nothing else.
This kills the duplication and means the outcome logic lives in exactly one place.

Outcome → toast + edit, driven by `handle_decision`'s return:
- `None` (unknown post) → toast `"Post not found"`, no edit.
- post already decided (returned unchanged, status ≠ the new decision's target) → toast
  `"Already {status}"`, still reconcile the message markup (idempotent edit) so a stale
  button disappears.
- fresh decision → toast `"✅ Approved"` / `"❌ Rejected"`, edit the message.

### 2. Edit the message on decision — `TelegramNotifier`
Add `mark_decided(cb_message: dict, decision: str) -> None`:
- Detect the message type from the callback message: `"photo" in cb_message` →
  `editMessageCaption` (new caption = original + `\n\n✅ Approved` / `❌ Rejected`);
  else → `editMessageText` (same append). Chat + message id come from `cb_message`.
- Remove the inline keyboard in the same call (`reply_markup` omitted / empty) so the
  buttons can't be tapped again.
- **Graceful failure:** Telegram rejects edits on messages older than 48h and returns 400
  on a no-op edit. Wrap in try/except on the httpx response; log and move on — a failed
  cosmetic edit must never break decision recording (the `Feedback` row is already
  committed by the time we edit).

*Original caption for the append:* pull it from `cb_message["caption"]`/`cb_message["text"]`
(Telegram includes it) — no need to re-render or store it.

### 3. Accurate toast + edge cases
Covered by §1's outcome mapping. Explicitly handle: unknown post, double-tap
(idempotent — already guaranteed by `handle_decision`, now also reflected in UI), expired
/ too-old message (edit fails gracefully), unparseable callback data (already ignored).

### 4. Structured logging on the decision path
Consistent, greppable log lines through `process_callback` and `handle_decision`:
`post_id`, parsed `decision`, resulting `status`, and edit success/failure — one line per
callback. No secrets (never log tokens or full update payloads). Keep it stdlib `logging`
with structured `%`-args, matching the existing style — no new logging dependency.

### 5. Tests — `tests/` (offline)
- A fake notifier capturing `mark_decided` / `answer_callback` calls, and fake `cb` update
  dicts (photo message and text message variants).
- `process_callback`: fresh approve → decision applied, message edited with ✅, toast
  `"✅ Approved"`; fresh reject → ❌; **double-tap** → second call is a no-op decision, toast
  `"Already …"`; **unknown post id** → `"Post not found"`, no edit.
- `mark_decided`: picks `editMessageCaption` for a photo message, `editMessageText` for a
  text message; a simulated 400 from the edit is swallowed (no raise).
- Suite stays fully offline (no real Telegram, no network).

## Files
- Modify: `app/pipeline/review.py` (add `process_callback`), `app/notifier/telegram.py`
  (add `mark_decided`; `_poll` calls `process_callback`), `app/main.py` (webhook calls
  `process_callback`), `tests/test_pipeline.py` (+ a small telegram test module if cleaner),
  `SCRIPTS_REFERENCE.md` if any run steps change.
- Add: none required.
- **No DB shape change ⇒ no migration.**

## Verification
1. `ruff check . && mypy app && pytest` — green, offline.
2. **Real round-trip** (manual, test chat + `python -m app.notifier.telegram poll`):
   approve → the photo caption gains ✅ and the buttons vanish; reject → ❌; the toast text
   matches; a second tap on the same (now buttonless) message, if forced, yields
   `"Already …"` and changes nothing.
3. **Webhook parity:** the same behavior via `/telegram/webhook` (secret validated) — both
   transports go through `process_callback`, so they can't diverge.
4. **Resilience:** editing a >48h-old message logs a warning and does not 500 the webhook;
   the `Feedback` row is still recorded.

## Out of scope (later milestones)
End-to-end slot draining / publish idempotency dry-run (M3); Postgres + `alembic upgrade`
on Railway (M4); real network publishers, inline "approve with edits", more review channels
(post-v1). No message-id persistence and no reminder/expiry sweeper this milestone —
add only if a real need appears.

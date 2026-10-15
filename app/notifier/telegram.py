"""Telegram review adapter — DMs suggestions with inline Approve/Reject buttons.

Callback data:
  ``approve:<post_id>``            → approve the post (also 📤 Publish after all on a kept card)
  ``keep:<post_id>``               → 📚 Keep: approved for learning only, never published
  ``reject:<post_id>``             → show reason-picker chips
  ``reason:<post_id>:<reason>``    → reject with the given reason (or "skip");
                                     ``ban_image`` also bans the photo from future batches
  ``almost:<post_id>``             → 🤏 Almost: show the "What's off?" chips (no ban chip)
  ``almostr:<post_id>:<reason>``   → apply Almost with the chip (or "skip")
  ``retry:<post_id>``              → 🔁 re-caption an Almost'd post's image as a new card
  ``distill:apply|discard:<token>`` → owner's answer to a /distill brand.md proposal
  ``fix:<post_id>``                → ForceReply prompt; the owner's reply (``#<id>`` in the
                                     prompt text — stateless) replaces the caption and approves

Run modes (CLI):
    python -m app.notifier.telegram poll           # long-poll (no public URL needed)
    python -m app.notifier.telegram set-webhook    # register the webhook
    python -m app.notifier.telegram delete-webhook
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import distill, learning, prefs, stock
from app import scheduler as sched
from app.config import Settings, get_settings
from app.llm import CaptionError
from app.models import BannedImage, Decision, Post, PostStatus
from app.pipeline import review
from app.pipeline.review import apply_edit, handle_decision
from app.render import image_path, render_full_caption

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
# Telegram photo captions are capped at 1024 chars; longer captions go in a follow-up text.
_PHOTO_CAPTION_LIMIT = 1024

# Reject-reason chips shown after the first ❌ tap.
_REJECT_REASONS: dict[str, str] = {
    "voice": "🗣 Voice",
    "hebrew": "🇮🇱 Hebrew",
    "image": "🖼 Image",
    "boring": "😴 Boring",
}
# Rejecting with this reason also bans the image from future suggestions.
_BAN_REASON = "ban_image"

# The ✏️ Fix prompt. Stateless: the post id and the card's message id travel in the text, and
# process_message recognises the owner's reply by this marker + _EDIT_REF.
_EDIT_PROMPT = (
    "Reply to this message with the corrected Hebrew caption (post #{post_id} · card {card}). "
    'Start with "en:" to fix the English instead.'
)
_EDIT_MARKER = "Reply to this message with the corrected"
_EDIT_REF = re.compile(r"post #(\d+) · card (\d+)")

# Shown in Telegram's "/" menu (setMyCommands) and by /help. Order = menu order.
_COMMAND_HELP: list[tuple[str, str]] = [
    ("status", "pipeline counts, queue depth, stock"),
    ("pending", "resend every suggestion awaiting review"),
    ("queue", "approved posts in posting order"),
    ("generate", "generate a batch now: /generate [n]"),
    ("postnow", "publish next in queue, or /postnow <id>"),
    ("requeue", "put a failed post back: /requeue <id>"),
    ("stock", "stock library counts; /stock unban <file>"),
    ("schedule", "view / change slots: /schedule post 12:00,18:00 · generate 09:00 · reset"),
    ("mode", "/mode learning (✅ = Keep, nothing posts) or /mode live"),
    ("distill", "propose a brand.md revision from recent feedback"),
    ("help", "this list"),
]


class TelegramNotifier:
    """Concrete :class:`app.notifier.base.Notifier` backed by the Telegram Bot API."""

    # Set by the app lifespan; None in the polling CLI (prefs still save, jobs apply on boot).
    scheduler: sched.CuteScheduler | None = None

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def _url(self, method: str) -> str:
        return _API.format(token=self._settings.telegram_bot_token, method=method)

    async def send_suggestion(self, post: Post) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            logger.warning("Telegram not configured — skipping send for post %s.", post.id)
            return
        caption = render_full_caption(post, self._settings)
        photo_path = image_path(post, self._settings)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{post.id}"},
                    {"text": "📚 Keep", "callback_data": f"keep:{post.id}"},
                    {"text": "✏️ Fix", "callback_data": f"fix:{post.id}"},
                ],
                [
                    {"text": "🤏 Almost", "callback_data": f"almost:{post.id}"},
                    {"text": "❌ Reject", "callback_data": f"reject:{post.id}"},
                ],
            ]
        }
        async with httpx.AsyncClient(timeout=15) as client:
            if len(caption) <= _PHOTO_CAPTION_LIMIT:
                resp = await client.post(
                    self._url("sendPhoto"),
                    data={
                        "chat_id": self._settings.telegram_chat_id,
                        "caption": caption,
                        "reply_markup": json.dumps(keyboard),
                    },
                    files={"photo": photo_path.read_bytes()},
                )
            else:
                # Caption too long for a photo caption — send the photo bare, then the full
                # text with the controls, so the buttons always sit with the full caption.
                await client.post(
                    self._url("sendPhoto"),
                    data={"chat_id": self._settings.telegram_chat_id},
                    files={"photo": photo_path.read_bytes()},
                )
                resp = await client.post(
                    self._url("sendMessage"),
                    json={
                        "chat_id": self._settings.telegram_chat_id,
                        "text": caption,
                        "reply_markup": keyboard,
                    },
                )
            if not resp.json().get("ok"):
                logger.warning(
                    "send_suggestion failed for post %s: %s",
                    post.id,
                    resp.json().get("description"),
                )

    async def send_message(self, text: str) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            logger.warning("Telegram not configured — skipping send_message.")
            return
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                self._url("sendMessage"),
                json={"chat_id": self._settings.telegram_chat_id, "text": text},
            )

    async def set_my_commands(self) -> None:
        """Register the command list so Telegram shows its native Menu button."""
        if not self._settings.telegram_bot_token:
            return
        commands = [{"command": c, "description": d} for c, d in _COMMAND_HELP]
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(self._url("setMyCommands"), json={"commands": commands})
            if not resp.json().get("ok"):
                logger.warning("setMyCommands rejected: %s", resp.json().get("description"))
        except Exception as exc:  # noqa: BLE001 — a menu glitch must never block startup
            logger.warning("setMyCommands failed: %s", exc)

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                self._url("answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
            )

    async def mark_decided(
        self, cb_message: dict, post: Post, reason: str | None = None, almost: bool = False
    ) -> None:
        """Edit the reviewed card; leave the reversal buttons while the verdict is reversible."""
        pid = post.id
        if post.status == PostStatus.APPROVED:
            label = "✅ Approved"
            row = [_btn("📚 Keep instead", f"keep:{pid}"), _btn("↩︎ Reject", f"reject:{pid}")]
        elif post.status == PostStatus.KEPT:
            label = "📚 Kept (learning only)"
            row = [
                _btn("📤 Publish after all", f"approve:{pid}"),
                _btn("↩︎ Reject", f"reject:{pid}"),
            ]
        elif post.status == PostStatus.REJECTED:
            label = ("🤏 Almost" if almost else "❌ Rejected") + (f" ({reason})" if reason else "")
            row = [_btn("↩︎ Approve", f"approve:{pid}")]
            if almost:
                row.insert(0, _btn("🔁 Retry", f"retry:{pid}"))
        else:
            label = "✅ Published" if post.status == PostStatus.PUBLISHED else f"[{post.status}]"
            row = []
        await self._edit_card(cb_message, label, [row] if row else [])

    async def mark_retried(self, cb_message: dict, new_id: int) -> None:
        """Replace the Almost card's 🔁 Retry with a pointer to the new card."""
        await self._edit_card(cb_message, f"🔁 retried → #{new_id}", [])

    async def _edit_card(self, cb_message: dict, suffix: str, rows: list[list[dict]]) -> None:
        """Append ``suffix`` to the card's caption/text and replace its buttons with ``rows``."""
        message_id = cb_message["message_id"]
        is_photo = "photo" in cb_message
        endpoint = "editMessageCaption" if is_photo else "editMessageText"
        body_key = "caption" if is_photo else "text"
        original = cb_message.get(body_key, "")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    self._url(endpoint),
                    json={
                        "chat_id": cb_message["chat"]["id"],
                        "message_id": message_id,
                        body_key: f"{original}\n\n{suffix}",
                        "reply_markup": {"inline_keyboard": rows},
                    },
                )
            if not resp.json().get("ok"):
                logger.warning(
                    "card edit rejected: message=%s description=%s",
                    message_id,
                    resp.json().get("description"),
                )
        except Exception as exc:
            logger.warning("card edit failed: message=%s error=%s", message_id, exc)

    async def prompt_edit(self, cb_message: dict, post_id: int) -> None:
        """Ask the owner for the corrected caption (ForceReply; state lives in the text)."""
        text = _EDIT_PROMPT.format(post_id=post_id, card=cb_message["message_id"])
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                self._url("sendMessage"),
                json={
                    "chat_id": cb_message["chat"]["id"],
                    "text": text,
                    "reply_markup": {
                        "force_reply": True,
                        "input_field_placeholder": "he, or en: …",
                    },
                },
            )

    async def send_distill(self, proposal: distill.Proposal) -> None:
        """DM a brand.md proposal with ✅ Apply / ❌ Discard."""
        diff = proposal.diff
        if len(diff) > _DISTILL_DIFF_LIMIT:
            diff = diff[:_DISTILL_DIFF_LIMIT] + "\n… (diff truncated; Apply writes the full file)"
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Apply", "callback_data": f"distill:apply:{proposal.token}"},
                    {"text": "❌ Discard", "callback_data": f"distill:discard:{proposal.token}"},
                ]
            ]
        }
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                self._url("sendMessage"),
                json={
                    "chat_id": self._settings.telegram_chat_id,
                    "text": f"{proposal.rationale}\n\n{diff}",
                    "reply_markup": keyboard,
                },
            )

    async def mark_distilled(self, cb_message: dict, label: str) -> None:
        """Replace the proposal's buttons with the outcome."""
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                self._url("editMessageText"),
                json={
                    "chat_id": cb_message["chat"]["id"],
                    "message_id": cb_message["message_id"],
                    "text": f"{cb_message.get('text', '')}\n\n{label}",
                    "reply_markup": {"inline_keyboard": []},
                },
            )

    async def show_edited(self, chat_id: int, card_id: int, post: Post) -> None:
        """Re-render the review card with the final caption + ``✅ Approved (edited)``."""
        body = f"{render_full_caption(post, self._settings)}\n\n✅ Approved (edited)"
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "↩︎ Reject", "callback_data": f"reject:{post.id}"},
                    {"text": "✏️ Fix", "callback_data": f"fix:{post.id}"},
                ]
            ]
        }
        # The card is a photo (caption) or, for long captions, plain text — try both.
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for endpoint, key in (
                    ("editMessageCaption", "caption"),
                    ("editMessageText", "text"),
                ):
                    resp = await client.post(
                        self._url(endpoint),
                        json={
                            "chat_id": chat_id,
                            "message_id": card_id,
                            key: body,
                            "reply_markup": keyboard,
                        },
                    )
                    if resp.json().get("ok"):
                        return
            logger.warning("show_edited rejected for card %s: %s", card_id, resp.json())
        except Exception as exc:
            logger.warning("show_edited failed: card=%s error=%s", card_id, exc)

    async def show_reason_picker(
        self, cb_message: dict, post_id: int, almost: bool = False
    ) -> None:
        """Edit the card in place to show the reason chips (❌ Reject or 🤏 Almost)."""
        prefix = "almostr" if almost else "reason"
        chips = [_btn(label, f"{prefix}:{post_id}:{r}") for r, label in _REJECT_REASONS.items()]
        last = [_btn("Skip", f"{prefix}:{post_id}:skip")]
        if not almost:  # the image was fine by definition on an Almost
            last.insert(0, _btn("🚫 Ban image", f"reason:{post_id}:{_BAN_REASON}"))
        await self._edit_card(cb_message, "What's off?" if almost else "Why reject?", [chips, last])


def _btn(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


_DISTILL_DIFF_LIMIT = 3000  # keeps rationale + diff inside Telegram's 4096-char message cap
_EDITABLE = (PostStatus.SUGGESTED, PostStatus.APPROVED)
_TERMINAL_CB = {PostStatus.PUBLISHING, PostStatus.PUBLISHED, PostStatus.FAILED}


# ─────────────────────────────── status helper ───────────────────────────────


async def _build_status_summary(session: AsyncSession, settings: Settings) -> str:
    rows = (await session.execute(select(Post.status, func.count()).group_by(Post.status))).all()
    counts: dict[PostStatus, int] = {r[0]: r[1] for r in rows}
    recent = (
        await session.scalars(
            select(Post)
            .where(Post.status == PostStatus.PUBLISHED)
            .order_by(Post.published_at.desc())
            .limit(5)
        )
    ).all()
    lines = []
    if await prefs.mode(session) == prefs.LEARNING:
        lines.append("🎓 LEARNING MODE — nothing publishes\n")
    lines.append("Pipeline status")
    for s in PostStatus:
        n = counts.get(s, 0)
        if n:
            lines.append(f"  {s}: {n}")
    queue_depth = counts.get(PostStatus.APPROVED, 0) + counts.get(PostStatus.PUBLISHING, 0)
    lines.append(f"\nQueue depth: {queue_depth}")
    lines.append(stock.summary_line(await stock.counts(session, settings)))
    lines.append(await _learning_line(session, settings))
    if recent:
        lines.append("\nLast published:")
        for p in recent:
            ts = p.published_at.strftime("%Y-%m-%d %H:%M UTC") if p.published_at else "?"
            lines.append(f"  #{p.id} — {ts}")
    return "\n".join(lines)


async def _learning_line(session: AsyncSession, settings: Settings) -> str:
    """The /status learning line — the verdict counts that tell the owner when to go live."""
    if not settings.learning_enabled:
        return "learning: off"
    n = await learning.verdict_counts(session, settings)
    ctx = await learning.build_context(session, settings)
    return (
        f"learning: {n.get(Decision.APPROVE, 0)} ✅ · {n.get(Decision.ALMOST, 0)} 🤏 · "
        f"{n.get(Decision.REJECT, 0)} ❌ (last {settings.learning_window_days}d)\n"
        f"  prompt sees: {ctx.summary()}"
    )


# ─────────────────────────────── command handlers ────────────────────────────


async def _cmd_status(
    session: AsyncSession, notifier: TelegramNotifier, args: str, settings: Settings
) -> None:
    await notifier.send_message(await _build_status_summary(session, settings))


async def _cmd_help(
    session: AsyncSession, notifier: TelegramNotifier, args: str, settings: Settings
) -> None:
    lines = ["Commands:"] + [f"/{c} — {d}" for c, d in _COMMAND_HELP]
    lines.append(
        "\nReview cards: ✅ Approve (publish) · 📚 Keep (good, learning only — never posts)"
        ' · ✏️ Fix (reply with the corrected Hebrew caption; "en:" prefix for English)'
        " · 🤏 Almost (right direction, flawed — pick what's off, then 🔁 Retry the same photo)"
        " · ❌ Reject (pick a reason; 🚫 Ban image retires the photo)."
    )
    lines.append("Send a photo to add it to the stock library.")
    await notifier.send_message("\n".join(lines))


async def _cmd_stock(
    session: AsyncSession, notifier: TelegramNotifier, args: str, settings: Settings
) -> None:
    sub, _, ref = args.partition(" ")
    if sub == "unban":
        if await stock.unban_image(session, ref.strip()):
            await session.commit()
            await notifier.send_message(f"{ref.strip()} is back in the pool.")
        else:
            await notifier.send_message(
                f"{ref.strip() or '<file>'} is not banned. Usage: /stock unban <file>"
            )
        return
    c = await stock.counts(session, settings)
    banned = (
        await session.scalars(select(BannedImage.image_ref).order_by(BannedImage.image_ref))
    ).all()
    lines = [stock.summary_line(c)]
    if banned:
        lines.append("Banned: " + ", ".join(banned))
    await notifier.send_message("\n".join(lines))


async def _cmd_generate(
    session: AsyncSession, notifier: TelegramNotifier, args: str, settings: Settings
) -> None:
    from app.pipeline import generate
    from app.pipeline import review as _rev

    n = int(args) if args.isdigit() else settings.batch_size
    posts = await generate.generate_batch(session, n=n)
    await _rev.send_for_review(posts, notifier)
    await notifier.send_message(f"Generated {len(posts)} post(s) — check your review DMs.")
    if warning := await stock.low_stock_message(session, settings, len(posts), n):
        await notifier.send_message(warning)


async def _cmd_postnow(
    session: AsyncSession, notifier: TelegramNotifier, args: str, settings: Settings
) -> None:
    from app.pipeline import publish

    if await prefs.mode(session) == prefs.LEARNING:
        await notifier.send_message("Learning mode — /mode live to publish.")
        return
    if args.isdigit():
        post = await publish.publish_by_id(session, int(args))
        if post is None:
            await notifier.send_message(f"Post #{args} not found.")
        elif post.status == PostStatus.PUBLISHED:
            await notifier.send_message(f"Post #{post.id} published ✅")
        elif post.status == PostStatus.FAILED:
            await notifier.send_message(f"Post #{post.id} publish failed.")
        elif post.status == PostStatus.KEPT:
            await notifier.send_message(
                f"Post #{post.id} is kept (learning only) — tap 📤 on its card"
            )
        else:
            await notifier.send_message(f"Post #{post.id} is {post.status}, not approved.")
    else:
        post = await publish.publish_next(session)
        if post is None:
            await notifier.send_message("Queue is empty — nothing to publish.")
        elif post.status == PostStatus.PUBLISHED:
            await notifier.send_message(f"Post #{post.id} published ✅")
        else:
            await notifier.send_message(f"Post #{post.id} publish failed.")


async def _cmd_mode(
    session: AsyncSession, notifier: TelegramNotifier, args: str, settings: Settings
) -> None:
    current = await prefs.mode(session)
    want = args.lower().strip()
    if not want:
        await notifier.send_message(f"Mode: {current}")
        return
    if want not in (prefs.LIVE, prefs.LEARNING):
        await notifier.send_message("Usage: /mode learning | /mode live")
        return
    await prefs.set(session, prefs.MODE, want)
    scheduler = notifier.scheduler
    if scheduler is not None:
        sched.apply_mode(scheduler, want)
    if want == prefs.LEARNING:
        n = await session.scalar(
            select(func.count()).select_from(Post).where(Post.status == PostStatus.APPROVED)
        )
        queued = f" {n} queued post(s) stay queued." if n else ""
        await notifier.send_message(
            "🎓 Learning mode — ✅ now means 📚 Keep, posting slots paused, "
            f"/postnow disabled.{queued}"
        )
    else:
        await notifier.send_message("🚀 Live — posting slots resumed, ✅ publishes again.")


_SCHEDULE_USAGE = (
    "Usage: /schedule · /schedule post 12:00,18:00 (1–6 HH:MM) · "
    '/schedule generate 09:00 | "0 9 * * 1-5" · /schedule reset'
)


def _fmt_next(when: Any) -> str:
    return when.strftime("%a %d %b %H:%M") if when is not None else "paused"


async def _schedule_view(
    session: AsyncSession, notifier: TelegramNotifier, settings: Settings
) -> str:
    cron = await prefs.generation_cron(session, settings)
    slots = await prefs.posting_slots(session, settings)
    lines = [f"Schedule ({settings.schedule_tz})", f"generate: {cron}"]
    runs = sched.next_runs(notifier.scheduler) if notifier.scheduler is not None else {}
    if "generation" in runs:
        lines.append(f"  next: {_fmt_next(runs['generation'])}")
    lines.append(f"post: {prefs.format_slots(slots)}")
    for hh, mm in slots:
        job_id = f"posting-{hh:02d}{mm:02d}"
        if job_id in runs:
            lines.append(f"  {hh:02d}:{mm:02d} next: {_fmt_next(runs[job_id])}")
    if await prefs.mode(session) == prefs.LEARNING:
        lines.append("🎓 learning mode — slots are paused")
    return "\n".join(lines)


async def _cmd_schedule(
    session: AsyncSession, notifier: TelegramNotifier, args: str, settings: Settings
) -> None:
    sub, _, rest = args.partition(" ")
    sub, rest = sub.lower(), rest.strip()
    scheduler = notifier.scheduler
    tz = settings.schedule_tz
    paused = await prefs.mode(session) == prefs.LEARNING

    if sub == "":
        await notifier.send_message(await _schedule_view(session, notifier, settings))
        return
    if sub == "post":
        try:
            slots = prefs.parse_slots(rest)
        except ValueError as exc:
            await notifier.send_message(f"{exc}\n{_SCHEDULE_USAGE}")
            return
        await prefs.set(session, prefs.POSTING_SLOTS, prefs.format_slots(slots))
        if scheduler is not None:
            sched.apply_slots(scheduler, slots, tz, paused=paused)
    elif sub == "generate":
        try:
            cron = prefs.parse_generation(rest)
        except ValueError:
            await notifier.send_message(f"Bad time or cron.\n{_SCHEDULE_USAGE}")
            return
        await prefs.set(session, prefs.GENERATION_CRON, cron)
        if scheduler is not None:
            sched.reschedule_generation(scheduler, cron, tz)
    elif sub == "reset":
        await prefs.delete(session, prefs.POSTING_SLOTS)
        await prefs.delete(session, prefs.GENERATION_CRON)
        if scheduler is not None:
            sched.apply_slots(scheduler, settings.posting_slots_list, tz, paused=paused)
            sched.reschedule_generation(scheduler, settings.generation_cron, tz)
    else:
        await notifier.send_message(_SCHEDULE_USAGE)
        return
    await notifier.send_message(await _schedule_view(session, notifier, settings))


async def _cmd_queue(
    session: AsyncSession, notifier: TelegramNotifier, args: str, settings: Settings
) -> None:
    posts = (
        await session.scalars(
            select(Post)
            .where(Post.status == PostStatus.APPROVED)
            .order_by(Post.queue_position.asc())
        )
    ).all()
    if not posts:
        await notifier.send_message("Queue is empty.")
        return
    lines = [f"Queue ({len(posts)} post(s)):"]
    for p in posts:
        preview = (p.caption_he[:60] + "…") if len(p.caption_he) > 60 else p.caption_he
        lines.append(f"  #{p.id} [pos {p.queue_position}] {preview}")
    await notifier.send_message("\n".join(lines))


async def _cmd_requeue(
    session: AsyncSession, notifier: TelegramNotifier, args: str, settings: Settings
) -> None:
    from app.pipeline import queue

    if not args.isdigit():
        await notifier.send_message("Usage: /requeue <id>")
        return
    post = await session.get(Post, int(args))
    if post is None:
        await notifier.send_message(f"Post #{args} not found.")
        return
    if post.status != PostStatus.FAILED:
        await notifier.send_message(f"Post #{post.id} is {post.status}, not failed.")
        return
    await queue.requeue(session, post)
    await session.commit()
    await notifier.send_message(f"Post #{post.id} requeued at position {post.queue_position}.")


async def _cmd_pending(
    session: AsyncSession, notifier: TelegramNotifier, args: str, settings: Settings
) -> None:
    from app.pipeline import review as _rev

    posts = (await session.scalars(select(Post).where(Post.status == PostStatus.SUGGESTED))).all()
    if not posts:
        await notifier.send_message("No pending posts.")
        return
    await notifier.send_message(f"Resending {len(posts)} pending post(s)…")
    await _rev.send_for_review(list(posts), notifier)


async def _cmd_distill(
    session: AsyncSession, notifier: TelegramNotifier, args: str, settings: Settings
) -> None:
    try:
        result = await distill.propose(session, settings)
    except CaptionError as exc:
        await notifier.send_message(f"Distill failed: {exc}")
        return
    if isinstance(result, str):
        await notifier.send_message(result)
    else:
        await notifier.send_distill(result)


_COMMANDS: dict[str, Any] = {
    "/status": _cmd_status,
    "/generate": _cmd_generate,
    "/postnow": _cmd_postnow,
    "/queue": _cmd_queue,
    "/requeue": _cmd_requeue,
    "/pending": _cmd_pending,
    "/stock": _cmd_stock,
    "/schedule": _cmd_schedule,
    "/mode": _cmd_mode,
    "/distill": _cmd_distill,
    "/help": _cmd_help,
    "/start": _cmd_help,
}


# ─────────────────────────────── photo upload ────────────────────────────────


async def _handle_photo_upload(notifier: TelegramNotifier, msg: dict, settings: Settings) -> None:
    photo = msg.get("photo", [])
    if not photo:
        return
    file_id = photo[-1]["file_id"]  # largest size is last
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(notifier._url("getFile"), params={"file_id": file_id})
        data = resp.json()
        if not data.get("ok"):
            logger.warning("getFile failed: %s", data)
            await notifier.send_message("Failed to retrieve photo from Telegram.")
            return
        file_path = data["result"]["file_path"]
        dl_url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
        img_resp = await client.get(dl_url)

    stock_dir = Path(settings.stock_images_dir)
    stock_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(file_path).name
    dest = stock_dir / filename
    dest.write_bytes(img_resp.content)
    await notifier.send_message(f"Photo saved to stock library as {filename}.")
    logger.info("Photo uploaded to stock: %s", dest)


# ─────────────────────────────── message dispatcher ──────────────────────────


async def _maybe_apply_edit(
    session: AsyncSession, notifier: TelegramNotifier, msg: dict, text: str
) -> bool:
    """If ``msg`` replies to our ✏️ Fix prompt, apply the edit. Returns True when handled."""
    prompt = msg.get("reply_to_message") or {}
    prompt_text = prompt.get("text") or ""  # a reply to a photo card has no "text" at all
    ref = _EDIT_REF.search(prompt_text)
    if not (prompt.get("from", {}).get("is_bot") and _EDIT_MARKER in prompt_text and ref):
        return False
    post_id, card_id = int(ref[1]), int(ref[2])
    lang = "he"
    if text[:3].lower() == "en:":
        lang, text = "en", text[3:].strip()
    if not text:
        await notifier.send_message("Empty caption — nothing changed.")
        return True
    pre = await session.get(Post, post_id)
    pre_state = (pre.status, pre.caption_he, pre.caption_en) if pre else None
    post = await apply_edit(session, post_id, lang, text)
    if post is None:
        await notifier.send_message(f"Post #{post_id} not found.")
    elif pre_state == (post.status, post.caption_he, post.caption_en):
        # Nothing happened: either the text matched, or the status refuses edits.
        reason = "is unchanged" if post.status in _EDITABLE else f"is {post.status}, can't edit"
        await notifier.send_message(f"Post #{post_id} {reason}.")
    else:
        await notifier.show_edited(msg["chat"]["id"], card_id, post)
        await notifier.send_message(f"Post #{post_id} updated ({lang}) ✅")
    return True


async def process_message(
    session: AsyncSession, notifier: TelegramNotifier, msg: dict, settings: Settings
) -> None:
    """Route an incoming plain message. Owner-gated commands and photo uploads are handled."""
    chat_id = str(msg.get("chat", {}).get("id", ""))
    is_owner = chat_id == str(settings.telegram_chat_id)

    text = (msg.get("text") or "").strip()
    if is_owner and text and await _maybe_apply_edit(session, notifier, msg, text):
        return
    if text.startswith("/"):
        if not is_owner:
            logger.warning("Ignoring command from non-owner chat %s.", chat_id)
            return
        cmd, _, args = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        handler = _COMMANDS.get(cmd)
        if handler:
            await handler(session, notifier, args.strip(), settings)
        return

    if msg.get("photo") and is_owner:
        await _handle_photo_upload(notifier, msg, settings)


# ─────────────────────────────── callback handler ────────────────────────────


async def process_callback(
    session: AsyncSession,
    notifier: TelegramNotifier,
    cb: dict,
    settings: Settings | None = None,
) -> None:
    """Route a Telegram callback: approve/keep, two-step reject/almost, retry, fix, distill."""
    data = cb.get("data", "")

    # ── /distill proposal: the only callback that writes a file, so owner-gated ──
    if data.startswith("distill:"):
        settings = settings or get_settings()
        _, _, rest = data.partition(":")
        action, _, token = rest.partition(":")
        if str(cb["message"]["chat"]["id"]) != str(settings.telegram_chat_id):
            logger.warning("Ignoring distill callback from non-owner chat.")
            return
        if action == "apply":
            label = distill.apply(token, settings)
        elif action == "discard":
            label = distill.discard(token)
        else:
            return
        await notifier.mark_distilled(cb["message"], label)
        await notifier.answer_callback(cb["id"], label)
        return

    # ── Step 1: ❌ / 🤏 tap → show reason picker ─────────────────────────────
    if data.startswith(("reject:", "almost:")):
        verb, _, raw_id = data.partition(":")
        if not raw_id.isdigit():
            return
        post_id = int(raw_id)
        post = await session.get(Post, post_id)
        if post is None:
            await notifier.answer_callback(cb["id"], "Post not found")
            return
        if post.status in _TERMINAL_CB:
            await notifier.answer_callback(cb["id"], "Can't change — already published")
            await notifier.mark_decided(cb["message"], post)
            return
        almost = verb == "almost"
        await notifier.answer_callback(cb["id"], "What's off?" if almost else "Why reject?")
        await notifier.show_reason_picker(cb["message"], post_id, almost=almost)
        logger.info("callback post_id=%s action=%s -> showing reason picker", post_id, verb)
        return

    # ── Step 2: chip tap → reject / almost with (optional) reason ────────────
    if data.startswith(("reason:", "almostr:")):
        parts = data.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            return
        almost = parts[0] == "almostr"
        post_id, reason_str = int(parts[1]), parts[2]
        reason: str | None = None if reason_str == "skip" else reason_str
        if almost and reason == _BAN_REASON:
            return  # never offered on Almost; a forged chip must not ban anything

        pre = await session.get(Post, post_id)
        pre_status = pre.status if pre is not None else None
        decision = "almost" if almost else "reject"
        post = await handle_decision(session, post_id, decision, reason=reason)

        if post is None:
            await notifier.answer_callback(cb["id"], "Post not found")
            return
        if pre_status in _TERMINAL_CB and pre_status == post.status:
            toast = "Can't change — already published"
        elif pre_status == post.status:
            toast = f"Already {post.status}"
        else:
            toast = "🤏 Almost" if almost else "❌ Rejected"
            toast += f" ({reason_str})" if reason else ""
        if reason == _BAN_REASON and post.status == PostStatus.REJECTED:
            await stock.ban_image(session, post.image_ref)
            await session.commit()
            toast = "❌ Rejected · 🚫 image banned"

        changed = pre_status != post.status
        await notifier.mark_decided(cb["message"], post, reason=reason, almost=almost and changed)
        logger.info(
            "callback post_id=%s decision=%s reason=%s status=%s toast=%r",
            post_id,
            decision,
            reason_str,
            post.status,
            toast,
        )
        await notifier.answer_callback(cb["id"], toast)
        return

    # ── ✏️ Fix: ask for the corrected caption ────────────────────────────────
    if data.startswith("fix:"):
        raw_id = data.removeprefix("fix:")
        post = await session.get(Post, int(raw_id)) if raw_id.isdigit() else None
        if post is None:
            await notifier.answer_callback(cb["id"], "Post not found")
        elif post.status not in _EDITABLE:
            await notifier.answer_callback(cb["id"], f"Can't edit — {post.status}")
        else:
            await notifier.answer_callback(cb["id"], "✏️ Reply with the corrected caption")
            await notifier.prompt_edit(cb["message"], post.id)
        return

    # ── 🔁 Retry an Almost'd post: same image, new card ───────────────────────
    if data.startswith("retry:"):
        raw_id = data.removeprefix("retry:")
        if not raw_id.isdigit():
            return
        post_id = int(raw_id)
        if (done := await review.retry_child(session, post_id)) is not None:
            await notifier.answer_callback(cb["id"], f"Already retried → #{done.id}")
            await notifier.mark_retried(cb["message"], done.id)
            return
        await notifier.answer_callback(cb["id"], "🔁 Generating a retry…")
        try:
            new = await review.retry(session, post_id, settings or get_settings())
        except CaptionError as exc:
            await notifier.send_message(f"Retry of #{post_id} failed: {exc}")
            return
        if new is None:
            await notifier.send_message(f"Post #{post_id} isn't an 🤏 Almost — nothing to retry.")
            return
        await review.send_for_review([new], notifier)
        # after the new card is out: a lost edit only leaves a stale button (answered above)
        await notifier.mark_retried(cb["message"], new.id)
        logger.info("callback post_id=%s action=retry -> #%s", post_id, new.id)
        return

    # ── Approve / 📚 Keep flow ────────────────────────────────────────────────
    parsed = parse_callback(data)
    if not parsed:
        return
    decision, post_id = parsed
    pre = await session.get(Post, post_id)
    pre_status = pre.status if pre is not None else None
    post = await handle_decision(session, post_id, decision)

    if post is None:
        toast = "Post not found"
    elif pre_status in _TERMINAL_CB and pre_status == post.status:
        toast = "Can't change — already published"
    elif pre_status == post.status:
        toast = f"Already {post.status}"
    else:
        if post.status == PostStatus.KEPT:
            toast = "📚 Kept (learning only)"
            if decision == "approve":
                toast = "📚 Kept — learning mode"
        else:
            toast = "✅ Approved"

    if post is not None:
        await notifier.mark_decided(cb["message"], post)

    logger.info(
        "callback post_id=%s decision=%s status=%s toast=%r",
        post_id,
        decision,
        post.status if post else None,
        toast,
    )
    await notifier.answer_callback(cb["id"], toast)


def parse_callback(data: str) -> tuple[str, int] | None:
    """Parse ``approve:<id>`` / ``keep:<id>`` into ``(decision, post_id)``; others route above."""
    action, _, raw_id = data.partition(":")
    if action not in ("approve", "keep") or not raw_id.isdigit():
        return None
    return action, int(raw_id)


# --------------------------------------------------------------------------- CLI


async def _set_webhook() -> None:
    settings = get_settings()
    url = f"{settings.telegram_webhook_base.rstrip('/')}/telegram/webhook"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _API.format(token=settings.telegram_bot_token, method="setWebhook"),
            json={"url": url, "secret_token": settings.telegram_webhook_secret},
        )
    logger.info("setWebhook -> %s", resp.json())


async def _delete_webhook() -> None:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _API.format(token=settings.telegram_bot_token, method="deleteWebhook"),
        )
    logger.info("deleteWebhook -> %s", resp.json())


async def _poll() -> None:
    """Long-poll for updates and route callbacks (local dev; no public URL needed)."""
    from app.db import SessionLocal

    settings = get_settings()
    notifier = TelegramNotifier(settings)
    await notifier.set_my_commands()
    offset = 0
    logger.info("Polling Telegram for updates… (Ctrl-C to stop)")
    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            resp = await client.get(
                _API.format(token=settings.telegram_bot_token, method="getUpdates"),
                params={"timeout": 30, "offset": offset},
            )
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                cb = update.get("callback_query")
                msg = update.get("message")
                if cb:
                    async with SessionLocal() as session:
                        await process_callback(session, notifier, cb)
                elif msg:
                    async with SessionLocal() as session:
                        await process_message(session, notifier, msg, settings)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "poll"
    runners = {
        "poll": _poll,
        "set-webhook": _set_webhook,
        "delete-webhook": _delete_webhook,
    }
    runner = runners.get(cmd)
    if runner is None:
        print(f"unknown command {cmd!r}; use: {', '.join(runners)}")
        raise SystemExit(2)
    asyncio.run(runner())


if __name__ == "__main__":
    main()

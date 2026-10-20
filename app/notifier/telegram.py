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
  ``distill:apply|discard:<token>`` → owner's answer to a /distill brand-text proposal
  ``fix:<post_id>``                → ForceReply prompt; the owner's reply (``#<id>`` in the
                                     prompt text — stateless) replaces the caption and approves

Run modes (CLI):
    python -m app.notifier.telegram poll           # long-poll (no public URL needed)
    python -m app.notifier.telegram set-webhook    # register the webhook
    python -m app.notifier.telegram delete-webhook
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import brands, distill, learning, prefs, stock
from app import scheduler as sched
from app.config import Settings, get_settings
from app.llm import CaptionError
from app.models import BannedImage, Brand, Decision, Post, PostStatus
from app.pipeline import queue, review
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

# One table drives Telegram's "/" menu (setMyCommands) and the grouped /help.
# (group, command, menu_label, usage). Row order = menu order (frequency, settings last).
# menu_label: one or two plain words, no emoji, ≤ 16 chars — Telegram lists it beside
# the command, so a sentence there is noise;
# "" keeps a command out of the menu (still typed, still in /help, most on the /console
# card). The menu only has to reach the card and the commands that take arguments.
# usage = args + detail, shown by /help only.
_MENU_LABEL_MAX = 16
_COMMANDS: list[tuple[str, str, str, str]] = [
    ("Pipeline", "console", "Console", "— pin a status card with buttons for everything below"),
    ("Pipeline", "status", "Status", "— counts, mode, verdicts"),
    ("Pipeline", "queue", "", "— approved posts; ▶ / ❌ per row"),
    ("Pipeline", "pending", "", "— resend the newest 5 open cards"),
    ("Pipeline", "generate", "Generate", "[n] — draft n posts now"),
    ("Pipeline", "postnow", "", "[id] — publish next, or post #id"),
    ("Pipeline", "requeue", "", "<id> — put a failed post back in the queue"),
    ("Library", "stock", "", "[unban <file>] — counts, banned list"),
    ("Settings", "schedule", "", "[post 12:00,18:00 | generate 09:00 | reset]"),
    ("Settings", "mode", "", "[learning|live] — learning: ✅ = Keep, nothing posts"),
    ("Settings", "distill", "", "— from recent feedback"),
    ("Settings", "brand", "", "— show the current brand text"),
    ("Settings", "help", "Help", ""),
]
_HELP_CARD = (
    "✅ Approve — publish · 📚 Keep — good, learning only, never posts · ✏️ Fix — reply with the "
    'corrected Hebrew caption ("en:" for English) · 🤏 Almost — right direction, pick what\'s '
    "off, then 🔁 Retry the same photo · ❌ Reject — pick a reason; 🚫 Ban image retires the photo"
)
_CALLBACK_DATA_MAX = 64  # bytes, Telegram's cap
# Output caps: a console tap must never flood the chat or blow Telegram's 4096-char message.
_PENDING_RESEND = 5  # /pending re-DMs only the newest cards; older ones stay open
_LIST_ROWS = 10  # /queue and /stock show this many rows, then "+N more"
_GENERATE_MAX = 10  # /generate n is clamped — each post is a paid vision call and a card
# The pinned console card: one tap = one command, output replaces the card text.
# /postnow is deliberately absent — publish from the ▶ button on a /queue row.
_CONSOLE_GRID = [
    ["Home", "Queue", "Pending"],
    ["Generate", "Stock", "Schedule"],
    ["Mode", "Status", "Help"],
]
_COUNTED = {"Queue": "queue", "Pending": "pending", "Stock": "stock"}  # label → counts key
_TOASTS = {"generate": "How many?", "home": "Home", "status": "Status…"}


class TelegramNotifier:
    """Concrete :class:`app.notifier.base.Notifier` backed by the Telegram Bot API."""

    # Set by the app lifespan; None in the polling CLI (prefs still save, jobs apply on boot).
    scheduler: sched.CuteScheduler | None = None

    def __init__(self, settings: Settings | None = None, chat_id: str | None = None) -> None:
        self._settings = settings or get_settings()
        # None = the env chat: only the process-wide instance, for boot warnings (M10).
        self._chat_id = self._settings.telegram_chat_id if chat_id is None else chat_id

    def for_brand(self, brand: Brand) -> TelegramNotifier:
        """A copy addressed to ``brand``'s owner chat (no chat on the row = sends skipped)."""
        bound = TelegramNotifier(self._settings, brand.telegram_chat_id or "")
        bound.scheduler = self.scheduler
        return bound

    def _url(self, method: str) -> str:
        return _API.format(token=self._settings.telegram_bot_token, method=method)

    async def send_suggestion(self, post: Post) -> None:
        if not self._settings.telegram_bot_token or not self._chat_id:
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
                        "chat_id": self._chat_id,
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
                    data={"chat_id": self._chat_id},
                    files={"photo": photo_path.read_bytes()},
                )
                resp = await client.post(
                    self._url("sendMessage"),
                    json={
                        "chat_id": self._chat_id,
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

    async def send_message(
        self,
        text: str,
        *,
        html: bool = False,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """DM the owner. ``html=True`` sends ``parse_mode=HTML`` (caller escapes values);
        ``keyboard`` is inline-keyboard rows."""
        if not self._settings.telegram_bot_token or not self._chat_id:
            logger.warning("Telegram not configured — skipping send_message.")
            return
        body: dict[str, Any] = {"chat_id": self._chat_id, "text": text}
        if html:
            body["parse_mode"] = "HTML"
        if keyboard:
            body["reply_markup"] = {"inline_keyboard": keyboard}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self._url("sendMessage"), json=body)
        if html and not resp.json().get("ok"):
            # Malformed markup must not swallow the message: resend as plain text.
            logger.warning("HTML sendMessage rejected: %s", resp.json().get("description"))
            plain = html_lib.unescape(re.sub(r"<[^>]+>", "", text))
            await self.send_message(plain, keyboard=keyboard)

    async def send_console(self, text: str, rows: list[list[dict[str, str]]]) -> None:
        """Send the console card (HTML + button grid) and pin it, silently."""
        chat = self._chat_id
        body = {
            "chat_id": chat,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": rows},
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self._url("sendMessage"), json=body)
            mid = (resp.json().get("result") or {}).get("message_id")
            if mid:
                await client.post(
                    self._url("pinChatMessage"),
                    json={"chat_id": chat, "message_id": mid, "disable_notification": True},
                )

    async def set_my_commands(self) -> None:
        """Register the "/" menu for the owner's chat; fall back to the default scope."""
        if not self._settings.telegram_bot_token:
            return
        commands = [
            {"command": cmd, "description": label} for _, cmd, label, _ in _COMMANDS if label
        ]
        owner = self._chat_id
        attempts: list[dict[str, Any]] = []
        if owner:
            attempts.append({"scope": {"type": "chat", "chat_id": owner}})
        attempts.append({})  # default scope — the menu must never disappear
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for extra in attempts:
                    resp = await client.post(
                        self._url("setMyCommands"), json={"commands": commands, **extra}
                    )
                    if resp.json().get("ok"):
                        return
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
        """DM a brand-text proposal with ✅ Apply / ❌ Discard."""
        diff = proposal.diff
        if len(diff) > _DISTILL_DIFF_LIMIT:
            diff = diff[:_DISTILL_DIFF_LIMIT] + "\n… (diff truncated; Apply writes the full text)"
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
                    "chat_id": self._chat_id,
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


async def _status_counts(session: AsyncSession, brand: Brand) -> dict[PostStatus, int]:
    rows = await session.execute(
        select(Post.status, func.count()).where(Post.brand_id == brand.id).group_by(Post.status)
    )
    return {r[0]: r[1] for r in rows.all()}


async def _console_counts(
    session: AsyncSession, brand: Brand, settings: Settings
) -> dict[str, int]:
    """The numbers the grid labels and the home card show: pending, queue, unused stock."""
    by = await _status_counts(session, brand)
    return {
        "pending": by.get(PostStatus.SUGGESTED, 0),
        "queue": by.get(PostStatus.APPROVED, 0) + by.get(PostStatus.PUBLISHING, 0),
        "stock": (await stock.counts(session, brand, settings))["unused"],
    }


def _console_rows(
    counts: dict[str, int] | None = None, back_only: bool = False
) -> list[list[dict[str, str]]]:
    if back_only:
        return [[_btn("‹ Menu", "con:home")]]

    def label(name: str) -> str:
        key = _COUNTED.get(name)
        return f"{name} {counts[key]}" if counts and key else name

    return [[_btn(label(name), f"con:{name.lower()}") for name in row] for row in _CONSOLE_GRID]


class _CardWriter(TelegramNotifier):
    """A notifier whose ``send_message`` edits the console card instead of sending.

    Output without buttons shows the full grid; output with buttons (``/mode`` confirm,
    ``/queue`` rows) shows only those plus "‹ Menu", which redraws the status card.

    Handlers stay untouched: a tap on the card runs the same handler as the typed
    command, and its output (plus any buttons it adds) replaces the card body, with
    the grid kept underneath. Everything else (cards, toasts) still goes out normally.
    """

    def __init__(
        self, base: TelegramNotifier, message_id: int, session: AsyncSession, brand: Brand
    ) -> None:
        super().__init__(base._settings, base._chat_id)
        self.scheduler = base.scheduler
        self._message_id = message_id
        self._session = session  # for the live counts on the grid labels
        self._brand = brand

    async def send_message(
        self,
        text: str,
        *,
        html: bool = False,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        if keyboard:  # a command with its own buttons gets a sub-menu: its buttons + one way back
            rows = keyboard + _console_rows(back_only=True)
        else:
            rows = _console_rows(await _console_counts(self._session, self._brand, self._settings))
        body: dict[str, Any] = {
            "chat_id": self._chat_id,
            "message_id": self._message_id,
            "text": text,
            "reply_markup": {"inline_keyboard": rows},
        }
        if html:
            body["parse_mode"] = "HTML"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self._url("editMessageText"), json=body)
        if html and not resp.json().get("ok"):
            logger.warning("HTML editMessageText rejected: %s", resp.json().get("description"))
            plain = html_lib.unescape(re.sub(r"<[^>]+>", "", text))
            await self.send_message(plain, keyboard=keyboard)


def _card_or_chat(
    notifier: TelegramNotifier, cb: dict, session: AsyncSession, brand: Brand
) -> TelegramNotifier:
    """Taps on the console card edit the card; taps elsewhere (a /queue DM) reply as usual."""
    markup = json.dumps(cb["message"].get("reply_markup") or {})
    if '"con:' not in markup:
        return notifier
    return _CardWriter(notifier, cb["message"]["message_id"], session, brand)


_BRAND_SHOW_LIMIT = 3500  # inside Telegram's 4096-char cap, with the <pre> wrapper
_DISTILL_DIFF_LIMIT = 3000  # keeps rationale + diff inside Telegram's 4096-char message cap
_EDITABLE = (PostStatus.SUGGESTED, PostStatus.APPROVED)
_TERMINAL_CB = {PostStatus.PUBLISHING, PostStatus.PUBLISHED, PostStatus.FAILED}


# ─────────────────────────────── status helper ───────────────────────────────


async def _build_status_summary(session: AsyncSession, brand: Brand, settings: Settings) -> str:
    """The detailed /status view — one function, so new lines (M10.3 cost) land here."""
    counts = await _status_counts(session, brand)
    recent = (
        await session.scalars(
            select(Post)
            .where(Post.brand_id == brand.id, Post.status == PostStatus.PUBLISHED)
            .order_by(Post.published_at.desc())
            .limit(5)
        )
    ).all()
    e = html_lib.escape
    lines = []
    if await prefs.mode(session, brand.id) == prefs.LEARNING:
        lines.append("🎓 <b>LEARNING MODE</b> — nothing publishes\n")
    lines.append("<b>Pipeline</b>")
    block = [f"{s:<10} {counts[s]}" for s in PostStatus if counts.get(s, 0)]
    queue_depth = counts.get(PostStatus.APPROVED, 0) + counts.get(PostStatus.PUBLISHING, 0)
    block.append(f"{'queue':<10} {queue_depth}")
    lines.append("<code>" + e("\n".join(block)) + "</code>")
    c = await stock.counts(session, brand, settings)
    lines.append("\n<b>Stock</b>\n" + e(stock.summary_line(c)))
    lines.append("\n<b>Learning</b>\n" + e(await _learning_line(session, brand, settings)))
    if recent:
        pub = []
        for p in recent:
            ts = p.published_at.strftime("%Y-%m-%d %H:%M UTC") if p.published_at else "?"
            pub.append(f"#{p.id} — {ts}")
        lines.append("\n<b>Last published</b>\n<code>" + e("\n".join(pub)) + "</code>")
    return "\n".join(lines)


async def _learning_line(session: AsyncSession, brand: Brand, settings: Settings) -> str:
    """The /status learning line — the verdict counts that tell the owner when to go live."""
    if not settings.learning_enabled:
        return "learning: off"
    n = await learning.verdict_counts(session, brand.id, settings)
    ctx = await learning.build_context(session, brand.id, settings)
    return (
        f"learning: {n.get(Decision.APPROVE, 0)} ✅ · {n.get(Decision.ALMOST, 0)} 🤏 · "
        f"{n.get(Decision.REJECT, 0)} ❌ (last {settings.learning_window_days}d)\n"
        f"  prompt sees: {ctx.summary()}"
    )


# ─────────────────────────────── command handlers ────────────────────────────


async def _cmd_status(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    await notifier.send_message(await _build_status_summary(session, brand, settings), html=True)


async def _build_home(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, settings: Settings
) -> str:
    """The card's default body: what needs the owner, not the raw status dump."""
    c = await _console_counts(session, brand, settings)
    e = html_lib.escape
    learning = await prefs.mode(session, brand.id) == prefs.LEARNING
    lines = [
        "🎓 <b>Learning mode</b> — nothing publishes" if learning else "🚀 <b>Live</b>",
    ]
    if not settings.publishing_live:
        lines.append("🔴 <b>DRY-RUN</b> — publishing is off, nothing reaches Instagram")
    lines.append(f"🟡 {c['pending']} waiting for review")
    runs = sched.next_runs(notifier.scheduler) if notifier.scheduler is not None else {}
    nxt = min((t for k, t in runs.items() if k.startswith("posting-") and t), default=None)
    queued = f"📬 {c['queue']} queued"
    lines.append(f"{queued} · next post {_fmt_next(nxt)}" if nxt else queued)
    lines.append(f"📷 {c['stock']} unused photos")
    lines.append("📚 " + e((await _learning_line(session, brand, settings)).split("\n")[0]))
    now = datetime.now(ZoneInfo(settings.schedule_tz)).strftime("%H:%M")
    lines.append(f"<i>updated {now}</i>")
    return "\n".join(lines)


async def _cmd_console(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    text = await _build_home(session, notifier, brand, settings)
    await notifier.send_console(
        text, _console_rows(await _console_counts(session, brand, settings))
    )


async def _cmd_home(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    await notifier.send_message(await _build_home(session, notifier, brand, settings), html=True)


async def _cmd_help(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    e = html_lib.escape
    lines = ["<b>Review a card</b>", e(_HELP_CARD)]
    for group in dict.fromkeys(g for g, *_ in _COMMANDS):
        lines.append(f"\n<b>{group}</b>")
        lines += [f"/{c} {e(u)}".rstrip() for g, c, _, u in _COMMANDS if g == group]
    lines.append("\nSend a photo to add it to the stock library.")
    await notifier.send_message("\n".join(lines), html=True)


async def _cmd_stock(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    sub, _, ref = args.partition(" ")
    if sub == "unban":
        if await stock.unban_image(session, brand.id, ref.strip()):
            await session.commit()
            await notifier.send_message(f"{ref.strip()} is back in the pool.")
        else:
            await notifier.send_message(
                f"{ref.strip() or '<file>'} is not banned. Usage: /stock unban <file>"
            )
        return
    c = await stock.counts(session, brand, settings)
    banned = (
        await session.scalars(
            select(BannedImage.image_ref)
            .where(BannedImage.brand_id == brand.id)
            .order_by(BannedImage.image_ref)
        )
    ).all()
    e = html_lib.escape
    lines = [e(stock.summary_line(c))]
    keyboard: list[list[dict[str, str]]] = []
    for ref in banned[:_LIST_ROWS]:
        data = f"unban:{ref}"
        if len(data.encode()) <= _CALLBACK_DATA_MAX:
            lines.append(f"🚫 <code>{e(ref)}</code>")
            keyboard.append([_btn(f"♻️ Unban {ref}", data)])
        else:  # too long for callback data — the typed command still works
            lines.append(f"🚫 <code>{e(ref)}</code> — /stock unban {e(ref)}")
    if len(banned) > _LIST_ROWS:
        more = len(banned) - _LIST_ROWS
        lines.append(f"… +{more} more banned — /stock unban &lt;file&gt; works for any of them")
    await notifier.send_message("\n".join(lines), html=True, keyboard=keyboard or None)


async def _cmd_generate(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    from app.pipeline import generate
    from app.pipeline import review as _rev

    n = int(args) if args.isdigit() else settings.batch_size
    if n > _GENERATE_MAX:
        await notifier.send_message(f"Capped at {_GENERATE_MAX} per /generate.")
        n = _GENERATE_MAX
    n = max(n, 1)
    posts = await generate.generate_batch(session, brand, n=n)
    await _rev.send_for_review(posts, notifier)
    await notifier.send_message(f"Generated {len(posts)} post(s) — check your review DMs.")
    if warning := await stock.low_stock_message(session, brand, settings, len(posts), n):
        await notifier.send_message(warning)


async def _cmd_postnow(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    from app.pipeline import publish

    if await prefs.mode(session, brand.id) == prefs.LEARNING:
        await notifier.send_message("Learning mode — /mode live to publish.")
        return
    if args.isdigit():
        post = await publish.publish_by_id(session, brand.id, int(args))
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
        post = await publish.publish_next(session, brand.id)
        if post is None:
            await notifier.send_message("Queue is empty — nothing to publish.")
        elif post.status == PostStatus.PUBLISHED:
            await notifier.send_message(f"Post #{post.id} published ✅")
        else:
            await notifier.send_message(f"Post #{post.id} publish failed.")


def _tick(on: bool) -> str:
    return " ✓" if on else ""


async def _cmd_mode(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    current = await prefs.mode(session, brand.id)
    want = args.lower().strip()
    if not want:  # confirmation keyboard: mode must not flip by accident
        await notifier.send_message(
            f"Mode: <b>{current}</b>\nSwitch to:",
            html=True,
            keyboard=[
                [
                    _btn(f"🎓 Learning{_tick(current == prefs.LEARNING)}", "mode:learning"),
                    _btn(f"🚀 Live{_tick(current == prefs.LIVE)}", "mode:live"),
                ]
            ],
        )
        return
    if want not in (prefs.LIVE, prefs.LEARNING):
        await notifier.send_message("Usage: /mode learning | /mode live")
        return
    await _set_mode(session, notifier, brand, want)


async def _set_mode(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, want: str
) -> None:
    await prefs.set(session, brand.id, prefs.MODE, want)
    scheduler = notifier.scheduler
    if scheduler is not None:
        sched.apply_mode(scheduler, want)
    if want == prefs.LEARNING:
        n = await queue.queue_length(session, brand.id)
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


_PICK_HOURS = range(7, 23)  # the hour grid: 07…22, four per row
_SIMPLE_CRON = re.compile(r"^0 (\d{1,2}) \* \* \*$")


def _schedule_keyboard(slots: list[tuple[int, int]], cron: str) -> list[list[dict[str, str]]]:
    """Tap-to-edit: ✖ per slot, + Add slot, generation hour, reset.
    ponytail: buttons work in whole hours; minutes still go through the typed command."""
    rows = [
        [_btn(f"✖ {h:02d}:{m:02d}", f"sch:del:{h:02d}{m:02d}") for h, m in slots[i : i + 3]]
        for i in range(0, len(slots), 3)
    ]
    m = _SIMPLE_CRON.match(cron)
    gen = f"⏰ Generate {int(m[1]):02d}:00" if m else "⏰ Generate at…"
    rows.append([_btn("＋ Add slot", "sch:pick:add"), _btn(gen, "sch:pick:gen")])
    rows.append([_btn("↺ Reset to defaults", "sch:reset")])
    return rows


def _hour_grid(what: str) -> list[list[dict[str, str]]]:
    hours = [_btn(f"{h:02d}:00", f"sch:{what}:{h}") for h in _PICK_HOURS]
    rows = [hours[i : i + 4] for i in range(0, len(hours), 4)]
    rows.append([_btn("‹ Schedule", "sch:view")])
    return rows


async def _schedule_view(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, settings: Settings
) -> tuple[str, list[list[dict[str, str]]]]:
    cron = await prefs.generation_cron(session, brand.id, settings)
    slots = await prefs.posting_slots(session, brand.id, settings)
    lines = [f"Schedule ({settings.schedule_tz})", f"generate: {cron}"]
    runs = sched.next_runs(notifier.scheduler) if notifier.scheduler is not None else {}
    if "generation" in runs:
        lines.append(f"  next: {_fmt_next(runs['generation'])}")
    lines.append(f"post: {prefs.format_slots(slots)}")
    for hh, mm in slots:
        job_id = f"posting-{hh:02d}{mm:02d}"
        if job_id in runs:
            lines.append(f"  {hh:02d}:{mm:02d} next: {_fmt_next(runs[job_id])}")
    if await prefs.mode(session, brand.id) == prefs.LEARNING:
        lines.append("🎓 learning mode — slots are paused")
    return "\n".join(lines), _schedule_keyboard(slots, cron)


async def _cmd_schedule(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    sub, _, rest = args.partition(" ")
    sub, rest = sub.lower(), rest.strip()
    scheduler = notifier.scheduler
    tz = settings.schedule_tz
    paused = await prefs.mode(session, brand.id) == prefs.LEARNING

    if sub == "":
        text, keyboard = await _schedule_view(session, notifier, brand, settings)
        await notifier.send_message(text, keyboard=keyboard)
        return
    if sub == "post":
        try:
            slots = prefs.parse_slots(rest)
        except ValueError as exc:
            await notifier.send_message(f"{exc}\n{_SCHEDULE_USAGE}")
            return
        await prefs.set(session, brand.id, prefs.POSTING_SLOTS, prefs.format_slots(slots))
        if scheduler is not None:
            sched.apply_slots(scheduler, slots, tz, paused=paused)
    elif sub == "generate":
        try:
            cron = prefs.parse_generation(rest)
        except ValueError:
            await notifier.send_message(f"Bad time or cron.\n{_SCHEDULE_USAGE}")
            return
        await prefs.set(session, brand.id, prefs.GENERATION_CRON, cron)
        if scheduler is not None:
            sched.reschedule_generation(scheduler, cron, tz)
    elif sub == "reset":
        await prefs.delete(session, brand.id, prefs.POSTING_SLOTS)
        await prefs.delete(session, brand.id, prefs.GENERATION_CRON)
        if scheduler is not None:
            sched.apply_slots(scheduler, settings.posting_slots_list, tz, paused=paused)
            sched.reschedule_generation(scheduler, settings.generation_cron, tz)
    else:
        await notifier.send_message(_SCHEDULE_USAGE)
        return
    text, keyboard = await _schedule_view(session, notifier, brand, settings)
    await notifier.send_message(text, keyboard=keyboard)


async def _schedule_callback(
    session: AsyncSession,
    notifier: TelegramNotifier,
    brand: Brand,
    cb: dict,
    arg: str,
    settings: Settings,
) -> None:
    """``sch:view`` / ``sch:pick:add|gen`` / ``sch:add:H`` / ``sch:del:HHMM`` / ``sch:gen:H`` /
    ``sch:reset`` — every edit is replayed through ``_cmd_schedule`` so one path applies it."""
    what, _, val = arg.partition(":")
    slots = await prefs.posting_slots(session, brand.id, settings)
    args, toast = "", ""
    if what == "pick" and val in ("add", "gen"):
        title = "Add a posting slot:" if val == "add" else "Generate daily at:"
        await notifier.send_message(title, keyboard=_hour_grid(val))
        return
    if what == "add" and val.isdigit():
        new = sorted(set(slots) | {(int(val), 0)})
        if len(new) > prefs.MAX_SLOTS:
            await notifier.answer_callback(cb["id"], f"Max {prefs.MAX_SLOTS} slots")
            return
        args, toast = f"post {prefs.format_slots(new)}", f"Added {int(val):02d}:00"
    elif what == "del" and len(val) == 4 and val.isdigit():
        gone = (int(val[:2]), int(val[2:]))
        keep = [x for x in slots if x != gone]
        if not keep:
            await notifier.answer_callback(cb["id"], "Keep at least one slot")
            return
        args, toast = f"post {prefs.format_slots(keep)}", f"Removed {val[:2]}:{val[2:]}"
    elif what == "gen" and val.isdigit():
        args, toast = f"generate {int(val):02d}:00", f"Generating daily at {int(val):02d}:00"
    elif what == "reset":
        args, toast = "reset", "Back to env defaults"
    elif what != "view":
        return
    await notifier.answer_callback(cb["id"], toast)
    await _cmd_schedule(session, notifier, brand, args, settings)


async def _cmd_queue(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    posts = (
        await session.scalars(
            select(Post)
            .where(Post.brand_id == brand.id, Post.status == PostStatus.APPROVED)
            .order_by(Post.queue_position.asc())
        )
    ).all()
    if not posts:
        await notifier.send_message("Queue is empty.")
        return
    e = html_lib.escape
    lines = [f"<b>Queue</b> ({len(posts)} post(s))"]
    keyboard = []
    for p in posts[:_LIST_ROWS]:
        preview = (p.caption_he[:60] + "…") if len(p.caption_he) > 60 else p.caption_he
        lines.append(f"#{p.id} · pos {p.queue_position} · {e(preview)}")
        keyboard.append(
            [_btn(f"▶ Post now #{p.id}", f"postnow:{p.id}"), _btn("❌ Drop", f"drop:{p.id}")]
        )
    if len(posts) > _LIST_ROWS:
        lines.append(f"… +{len(posts) - _LIST_ROWS} more, in order")
    await notifier.send_message("\n".join(lines), html=True, keyboard=keyboard)


async def _cmd_requeue(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    if not args.isdigit():
        await notifier.send_message("Usage: /requeue <id>")
        return
    post = await review.own_post(session, brand.id, int(args))
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
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    """List what awaits review; resend only on request (``/pending resend`` or the button)."""
    if args.strip().lower() == "resend":
        await _resend_pending(session, notifier, brand)
        return
    posts = (
        await session.scalars(
            select(Post)
            .where(Post.brand_id == brand.id, Post.status == PostStatus.SUGGESTED)
            .order_by(Post.id.desc())
        )
    ).all()
    if not posts:
        await notifier.send_message("No pending posts.")
        return
    e = html_lib.escape
    lines = [f"<b>{len(posts)} pending</b> — newest first"]
    for p in posts[:_LIST_ROWS]:
        preview = (p.caption_he[:60] + "…") if len(p.caption_he) > 60 else p.caption_he
        lines.append(f"#{p.id} · {e(preview)}")
    if len(posts) > _LIST_ROWS:
        lines.append(f"… +{len(posts) - _LIST_ROWS} more")
    n = min(len(posts), _PENDING_RESEND)
    await notifier.send_message(
        "\n".join(lines), html=True, keyboard=[[_btn(f"🔁 Resend newest {n}", "pend:resend")]]
    )


async def _resend_pending(session: AsyncSession, notifier: TelegramNotifier, brand: Brand) -> None:
    from app.pipeline import review as _rev

    pending = (Post.brand_id == brand.id, Post.status == PostStatus.SUGGESTED)
    total = await session.scalar(select(func.count()).select_from(Post).where(*pending))
    if not total:
        await notifier.send_message("No pending posts.")
        return
    latest = (
        await session.scalars(
            select(Post).where(*pending).order_by(Post.id.desc()).limit(_PENDING_RESEND)
        )
    ).all()
    tail = f" — resending the latest {len(latest)}…" if total > len(latest) else " — resending…"
    await notifier.send_message(f"<b>{total} pending post(s)</b>{tail}", html=True)
    await _rev.send_for_review(list(reversed(latest)), notifier)  # oldest first, newest on top


async def _cmd_distill(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    try:
        result = await distill.propose(session, settings, brand)
    except CaptionError as exc:
        await notifier.send_message(f"Distill failed: {exc}")
        return
    if isinstance(result, str):
        await notifier.send_message(result)
    else:
        await notifier.send_distill(result)


async def _cmd_brand(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, args: str, settings: Settings
) -> None:
    text = brands.text(brand)
    if len(text) > _BRAND_SHOW_LIMIT:
        text = text[:_BRAND_SHOW_LIMIT] + "\n… (truncated; brand_sync.py pull gives the full text)"
    body = f"<b>Brand text</b>\n<pre>{html_lib.escape(text)}</pre>"
    await notifier.send_message(body, html=True)


_HANDLERS: dict[str, Any] = {
    "/console": _cmd_console,
    "/home": _cmd_home,
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
    "/brand": _cmd_brand,
    "/help": _cmd_help,
    "/start": _cmd_help,
}


# ─────────────────────────────── photo upload ────────────────────────────────


async def _handle_photo_upload(
    notifier: TelegramNotifier, brand: Brand, msg: dict, settings: Settings
) -> None:
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

    stock_dir = stock.brand_dir(settings, brand.stock_prefix)
    stock_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(file_path).name
    dest = stock_dir / filename
    dest.write_bytes(img_resp.content)
    await notifier.send_message(f"Photo saved to stock library as {filename}.")
    logger.info("Photo uploaded to stock: %s", dest)


# ─────────────────────────────── message dispatcher ──────────────────────────


async def _maybe_apply_edit(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, msg: dict, text: str
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
    pre = await review.own_post(session, brand.id, post_id)
    pre_state = (pre.status, pre.caption_he, pre.caption_en) if pre else None
    post = await apply_edit(session, brand.id, post_id, lang, text)
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
    """Route an incoming message from a brand's owner chat: ✏️ Fix replies, commands, photos.

    The chat resolves to its brand; any other chat is ignored.
    """
    chat_id = msg.get("chat", {}).get("id")
    brand = await brands.by_chat_id(session, chat_id)
    if brand is None:
        logger.warning("Ignoring message from unknown chat %s.", chat_id)
        return
    notifier = notifier.for_brand(brand)

    text = (msg.get("text") or "").strip()
    if text and await _maybe_apply_edit(session, notifier, brand, msg, text):
        return
    if text.startswith("/"):
        cmd, _, args = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        handler = _HANDLERS.get(cmd)
        if handler:
            await handler(session, notifier, brand, args.strip(), settings)
        return

    if msg.get("photo"):
        await _handle_photo_upload(notifier, brand, msg, settings)


# ─────────────────────────────── callback handler ────────────────────────────


async def process_callback(
    session: AsyncSession,
    notifier: TelegramNotifier,
    cb: dict,
    settings: Settings | None = None,
) -> None:
    """Route a Telegram callback: approve/keep, two-step reject/almost, retry, fix, distill.

    The card's chat resolves to its brand; a tap from any other chat is ignored, and a post
    of another brand is "not found".
    """
    data = cb.get("data", "")
    settings = settings or get_settings()
    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
    brand = await brands.by_chat_id(session, chat_id)
    if brand is None:
        logger.warning("Ignoring callback from unknown chat %s.", chat_id)
        return
    notifier = notifier.for_brand(brand)

    # ── /distill proposal ──
    if data.startswith("distill:"):
        _, _, rest = data.partition(":")
        action, _, token = rest.partition(":")
        if action == "apply":
            label = await distill.apply(session, brand, token)
        elif action == "discard":
            label = distill.discard(token)
        else:
            return
        await notifier.mark_distilled(cb["message"], label)
        await notifier.answer_callback(cb["id"], label)
        return

    # ── console buttons (/queue, /stock, /mode): state-changing, so owner-gated ──
    if data.startswith(("con:", "gen:", "pend:", "sch:", "postnow:", "drop:", "unban:", "mode:")):
        await _console_callback(session, notifier, brand, cb, settings)
        return

    # ── Step 1: ❌ / 🤏 tap → show reason picker ─────────────────────────────
    if data.startswith(("reject:", "almost:")):
        verb, _, raw_id = data.partition(":")
        if not raw_id.isdigit():
            return
        post_id = int(raw_id)
        post = await review.own_post(session, brand.id, post_id)
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

        pre = await review.own_post(session, brand.id, post_id)
        pre_status = pre.status if pre is not None else None
        decision = "almost" if almost else "reject"
        post = await handle_decision(session, brand.id, post_id, decision, reason=reason)

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
            await stock.ban_image(session, brand.id, post.image_ref)
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
        post = await review.own_post(session, brand.id, int(raw_id)) if raw_id.isdigit() else None
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
        if (done := await review.retry_child(session, brand.id, post_id)) is not None:
            await notifier.answer_callback(cb["id"], f"Already retried → #{done.id}")
            await notifier.mark_retried(cb["message"], done.id)
            return
        await notifier.answer_callback(cb["id"], "🔁 Generating a retry…")
        try:
            new = await review.retry(session, brand, post_id, settings)
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
    pre = await review.own_post(session, brand.id, post_id)
    pre_status = pre.status if pre is not None else None
    post = await handle_decision(session, brand.id, post_id, decision)

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


async def _console_callback(
    session: AsyncSession, notifier: TelegramNotifier, brand: Brand, cb: dict, settings: Settings
) -> None:
    """``con:<cmd>`` / ``postnow:<id>`` / ``drop:<id>`` / ``unban:<ref>`` / ``mode:<…>``."""
    action, _, arg = cb.get("data", "").partition(":")
    notifier = _card_or_chat(notifier, cb, session, brand)
    if action == "sch":
        await _schedule_callback(session, notifier, brand, cb, arg, settings)
    elif action == "con" and arg == "generate":
        await notifier.answer_callback(cb["id"], _TOASTS["generate"])
        await notifier.send_message(
            "Draft how many?", keyboard=[[_btn(str(n), f"gen:{n}") for n in (1, 3, 5)]]
        )
    elif action == "con":
        handler = _HANDLERS.get(f"/{arg}") if arg != "console" else None
        if handler is not None:
            await notifier.answer_callback(cb["id"], _TOASTS.get(arg, f"{arg.capitalize()}…"))
            await handler(session, notifier, brand, "", settings)
    elif action == "gen" and arg.isdigit():
        await notifier.answer_callback(cb["id"], f"⏳ Drafting {arg}…")
        await notifier.send_message(f"⏳ Drafting {arg} post(s)… cards arrive below.")
        await _cmd_generate(session, notifier, brand, arg, settings)
    elif action == "pend" and arg == "resend":
        await notifier.answer_callback(cb["id"], "⏳ Resending…")
        await notifier.send_message("⏳ Resending the newest cards…")
        await _resend_pending(session, notifier, brand)
    elif action == "postnow" and arg.isdigit():
        await notifier.answer_callback(cb["id"], f"▶ Publishing #{arg}…")
        await _cmd_postnow(session, notifier, brand, arg, settings)
    elif action == "drop" and arg.isdigit():
        pre = await review.own_post(session, brand.id, int(arg))
        post = await handle_decision(session, brand.id, int(arg), "reject")
        if post is None:
            toast = "Post not found"
        elif pre is not None and pre.status in _TERMINAL_CB:
            toast = "Can't drop — already published"
        else:
            toast = f"❌ Dropped #{post.id}"
        await notifier.answer_callback(cb["id"], toast)
        await _cmd_queue(session, notifier, brand, "", settings)  # the list is stale now
    elif action == "unban" and arg:
        ok = await stock.unban_image(session, brand.id, arg)
        await session.commit()
        await notifier.answer_callback(cb["id"], "♻️ Back in the pool" if ok else "Not banned")
    elif action == "mode" and arg in (prefs.LEARNING, prefs.LIVE):
        await notifier.answer_callback(cb["id"], f"Mode → {arg}")
        await _set_mode(session, notifier, brand, arg)
    elif action == "mode" and arg == "cancel":
        await notifier.answer_callback(cb["id"], "Cancelled")


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

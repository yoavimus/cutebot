"""Telegram review adapter — DMs suggestions with inline Approve/Reject buttons.

Callback data:
  ``approve:<post_id>``            → approve the post
  ``reject:<post_id>``             → show reason-picker chips
  ``reason:<post_id>:<reason>``    → reject with the given reason (or "skip");
                                     ``ban_image`` also bans the photo from future batches
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

from app import distill, learning, stock
from app.config import Settings, get_settings
from app.models import BannedImage, Post, PostStatus
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
    ("distill", "propose a brand.md revision from recent feedback"),
    ("help", "this list"),
]


class TelegramNotifier:
    """Concrete :class:`app.notifier.base.Notifier` backed by the Telegram Bot API."""

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
                    {"text": "❌ Reject", "callback_data": f"reject:{post.id}"},
                    {"text": "✏️ Fix", "callback_data": f"fix:{post.id}"},
                ]
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

    async def mark_decided(self, cb_message: dict, post: Post, reason: str | None = None) -> None:
        """Edit the reviewed message; leave the opposite button while decision is reversible."""
        if post.status == PostStatus.APPROVED:
            label = "✅ Approved"
            keyboard: dict[str, Any] = {
                "inline_keyboard": [[{"text": "↩︎ Reject", "callback_data": f"reject:{post.id}"}]]
            }
        elif post.status == PostStatus.REJECTED:
            label = "❌ Rejected"
            if reason:
                label += f" ({reason})"
            keyboard = {
                "inline_keyboard": [[{"text": "↩︎ Approve", "callback_data": f"approve:{post.id}"}]]
            }
        else:
            label = "✅ Published" if post.status == PostStatus.PUBLISHED else f"[{post.status}]"
            keyboard = {"inline_keyboard": []}
        chat_id = cb_message["chat"]["id"]
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
                        "chat_id": chat_id,
                        "message_id": message_id,
                        body_key: f"{original}\n\n{label}",
                        "reply_markup": keyboard,
                    },
                )
            if not resp.json().get("ok"):
                logger.warning(
                    "mark_decided edit rejected: message=%s description=%s",
                    message_id,
                    resp.json().get("description"),
                )
        except Exception as exc:
            logger.warning("mark_decided failed: message=%s error=%s", message_id, exc)

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

    async def show_reason_picker(self, cb_message: dict, post_id: int) -> None:
        """Edit the message in-place to display reject-reason chips."""
        reason_row = [
            {"text": label, "callback_data": f"reason:{post_id}:{r}"}
            for r, label in _REJECT_REASONS.items()
        ]
        keyboard = {
            "inline_keyboard": [
                reason_row,
                [
                    {"text": "🚫 Ban image", "callback_data": f"reason:{post_id}:{_BAN_REASON}"},
                    {"text": "Skip", "callback_data": f"reason:{post_id}:skip"},
                ],
            ]
        }
        chat_id = cb_message["chat"]["id"]
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
                        "chat_id": chat_id,
                        "message_id": message_id,
                        body_key: f"{original}\n\nWhy reject?",
                        "reply_markup": keyboard,
                    },
                )
            if not resp.json().get("ok"):
                logger.warning(
                    "show_reason_picker edit rejected: message=%s description=%s",
                    message_id,
                    resp.json().get("description"),
                )
        except Exception as exc:
            logger.warning("show_reason_picker failed: message=%s error=%s", message_id, exc)


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
    lines = ["Pipeline status"]
    for s in PostStatus:
        n = counts.get(s, 0)
        if n:
            lines.append(f"  {s}: {n}")
    queue_depth = counts.get(PostStatus.APPROVED, 0) + counts.get(PostStatus.PUBLISHING, 0)
    lines.append(f"\nQueue depth: {queue_depth}")
    lines.append(stock.summary_line(await stock.counts(session, settings)))
    ctx = await learning.build_context(session, settings)
    lines.append("learning: " + ("off" if not settings.learning_enabled else ctx.summary()))
    if recent:
        lines.append("\nLast published:")
        for p in recent:
            ts = p.published_at.strftime("%Y-%m-%d %H:%M UTC") if p.published_at else "?"
            lines.append(f"  #{p.id} — {ts}")
    return "\n".join(lines)


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
        "\nReview cards: ✅ Approve · ❌ Reject (pick a reason; 🚫 Ban image retires the photo)"
        ' · ✏️ Fix (reply with the corrected Hebrew caption; "en:" prefix for English).'
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

    if args.isdigit():
        post = await publish.publish_by_id(session, int(args))
        if post is None:
            await notifier.send_message(f"Post #{args} not found.")
        elif post.status == PostStatus.PUBLISHED:
            await notifier.send_message(f"Post #{post.id} published ✅")
        elif post.status == PostStatus.FAILED:
            await notifier.send_message(f"Post #{post.id} publish failed.")
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
    from app.llm import CaptionError

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
    ref = _EDIT_REF.search(prompt.get("text") or "")
    if not (prompt.get("from", {}).get("is_bot") and _EDIT_MARKER in prompt["text"] and ref):
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
    """Parse a Telegram callback query and route to approve or two-step reject."""
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

    # ── Step 1: ❌ tap → show reason picker ──────────────────────────────────
    if data.startswith("reject:"):
        raw_id = data.removeprefix("reject:")
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
        await notifier.answer_callback(cb["id"], "Why reject?")
        await notifier.show_reason_picker(cb["message"], post_id)
        logger.info("callback post_id=%s action=reject -> showing reason picker", post_id)
        return

    # ── Step 2: reason tap → reject with (optional) reason ───────────────────
    if data.startswith("reason:"):
        parts = data.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            return
        post_id, reason_str = int(parts[1]), parts[2]
        reason: str | None = None if reason_str == "skip" else reason_str

        pre = await session.get(Post, post_id)
        pre_status = pre.status if pre is not None else None
        post = await handle_decision(session, post_id, "reject", reason=reason)

        if post is None:
            await notifier.answer_callback(cb["id"], "Post not found")
            return
        if pre_status in _TERMINAL_CB and pre_status == post.status:
            toast = "Can't change — already published"
        elif pre_status == post.status:
            toast = f"Already {post.status}"
        else:
            toast = "❌ Rejected" + (f" ({reason_str})" if reason else "")
        if reason == _BAN_REASON and post.status == PostStatus.REJECTED:
            await stock.ban_image(session, post.image_ref)
            await session.commit()
            toast = "❌ Rejected · 🚫 image banned"

        await notifier.mark_decided(cb["message"], post, reason=reason)
        logger.info(
            "callback post_id=%s decision=reject reason=%s status=%s toast=%r",
            post_id,
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

    # ── Approve flow ──────────────────────────────────────────────────────────
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
    """Parse ``approve:<id>`` into ``(decision, post_id)``. Reject/reason are handled separately."""
    action, _, raw_id = data.partition(":")
    if action != "approve" or not raw_id.isdigit():
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

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from .db import transaction
from .models import AdminGrant, AdminPhoneGrant, ChatDelivery, ChatMessage, PhoneWhitelist, User

log = logging.getLogger(__name__)
RETENTION_DAYS = 14
MAX_ATTEMPTS = 5


def reply_callback_data(message: ChatMessage) -> str:
    """Bind a reply button to this exact message, even if SQLite reuses its ID."""
    return f"chat-reply:{message.id}:{message.created_at:%Y%m%d%H%M%S%f}"


def _cutoff(now: datetime | None = None) -> datetime:
    value = now or datetime.now(UTC)
    value = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return value - timedelta(days=RETENTION_DAYS)


def create_message(session, user_id: str, body: str, maintainers: frozenset[int]) -> ChatMessage:
    message = ChatMessage(user_id=user_id, body=body)
    session.add(message)
    session.flush()
    session.add_all(ChatDelivery(message_id=message.id, maintainer_telegram_id=telegram_id)
                    for telegram_id in sorted(maintainers))
    return message


def _sender_note(session, user_id: str) -> str:
    """Show the sender's whitelist/admin labels only to message recipients."""
    user = session.get(User, user_id)
    if user is None:
        return ""
    whitelist = session.get(PhoneWhitelist, user.phone_fingerprint)
    admin = session.get(AdminGrant, user.telegram_id)
    phone_admin = session.get(AdminPhoneGrant, user.phone_fingerprint)
    whitelist_label = (whitelist.label or "").strip() if whitelist else ""
    admin_label = (admin.label or "").strip() if admin else ""
    if not admin_label and phone_admin and phone_admin.telegram_id == user.telegram_id:
        admin_label = (phone_admin.label or "").strip()
    labels = [label for label in (whitelist_label, admin_label) if label]
    return f"备注：({escape('/'.join(labels))})\n" if labels else ""


def purge_expired_chat_messages(factory, now: datetime | None = None) -> int:
    """Delete messages at least 14 days old; delivery rows cascade with them."""
    with transaction(factory) as session:
        result = session.execute(delete(ChatMessage).where(ChatMessage.created_at <= _cutoff(now)))
        return result.rowcount or 0


async def deliver_chat_messages(application, message_id: int | None = None) -> tuple[int, int]:
    """Deliver persisted messages to maintainers; safe to retry after restart."""
    lock = application.bot_data.setdefault("chat_delivery_lock", asyncio.Lock())
    if lock.locked():
        return 0, 0
    async with lock:
        factory = application.bot_data["session_factory"]
        purge_expired_chat_messages(factory)
        with transaction(factory) as session:
            query = (select(ChatDelivery, ChatMessage)
                     .join(ChatMessage, ChatMessage.id == ChatDelivery.message_id)
                     .where(ChatDelivery.delivered_at.is_(None), ChatDelivery.attempts < MAX_ATTEMPTS))
            if message_id is not None:
                query = query.where(ChatDelivery.message_id == message_id)
            rows = [(delivery, message, _sender_note(session, message.user_id))
                    for delivery, message in session.execute(query.order_by(ChatDelivery.id)).all()]
        sent = failed = 0
        for delivery, message, sender_note in rows:
            if application.bot_data.get("enforce_premium"):
                with transaction(factory) as session:
                    accounts = application.bot_data["accounts"]
                    if not accounts.is_registered(session, delivery.maintainer_telegram_id) or not accounts.is_maintainer(session, delivery.maintainer_telegram_id):
                        continue
            created = message.created_at if message.created_at.tzinfo else message.created_at.replace(tzinfo=UTC)
            text = (f"💬 <b>用户留言 #{message.id}</b>\n"
                    f"{sender_note}"
                    f"时间：{created.astimezone(ZoneInfo('Asia/Shanghai')):%Y-%m-%d %H:%M}（北京时间）\n\n"
                    f"{escape(message.body)}")
            error = None
            try:
                await application.bot.send_message(
                    delivery.maintainer_telegram_id, text, parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("回复用户", callback_data=reply_callback_data(message))
                    ]]),
                )
                sent += 1
            except TelegramError as exc:
                error = f"{type(exc).__name__}: {exc}"[:1000]
                failed += 1
            with transaction(factory) as session:
                current = session.get(ChatDelivery, delivery.id)
                if current is None:
                    continue
                current.attempts += 1
                current.last_error = error
                if error is None:
                    current.delivered_at = datetime.now(UTC)
            await asyncio.sleep(0.04)
        return sent, failed


async def maintain_chat_messages(context) -> None:
    try:
        removed = purge_expired_chat_messages(context.application.bot_data["session_factory"])
        sent, failed = await deliver_chat_messages(context.application)
        if removed or sent or failed:
            log.info("chat maintenance: removed=%s sent=%s failed=%s", removed, sent, failed)
    except Exception:
        log.exception("chat maintenance failed")

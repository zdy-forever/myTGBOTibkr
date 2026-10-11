"""Ephemeral announcement delivery; no bodies or delivery records are persisted."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select
from telegram.error import RetryAfter, TelegramError

from .db import transaction
from .models import NotificationPreference, User


async def publish_announcement(application, content: str, *, important: bool = False) -> tuple[int, int]:
    lock = application.bot_data.setdefault("announcement_publish_lock", asyncio.Lock())
    async with lock:
        factory = application.bot_data["session_factory"]
        with transaction(factory) as session:
            users = list(session.execute(select(User.id, User.telegram_id).where(User.is_active)))
        succeeded = failed = 0
        # Only pins created by this process are tracked. Never unpin unrelated messages.
        pins = application.bot_data.setdefault("important_announcement_pins", {}) if important else {}
        for user_id, telegram_id in users:
            message_id = None
            delivered = pinned = False
            for _attempt in range(5):
                with transaction(factory) as session:
                    user = session.get(User, user_id)
                    if user is None or not user.is_active or not user.phone_fingerprint:
                        break
                    if not important:
                        prefs = session.get(NotificationPreference, user_id)
                        if not prefs or not prefs.announcements_enabled:
                            break
                        if (application.bot_data.get("enforce_premium") and
                                not application.bot_data["accounts"].has_premium_access(session, telegram_id)):
                            break
                try:
                    if not delivered:
                        message = await application.bot.send_message(
                            telegram_id, f"{'📌 重要公告' if important else '📢 公告'}\n\n{content}")
                        delivered = True
                        if important:
                            message_id = message.message_id
                    if important:
                        if not pinned:
                            await application.bot.pin_chat_message(chat_id=telegram_id, message_id=message_id)
                            pinned = True
                            pins.setdefault(telegram_id, set()).add(message_id)
                        for old in list(pins.get(telegram_id, set()) - {message_id}):
                            await application.bot.unpin_chat_message(chat_id=telegram_id, message_id=old)
                            pins[telegram_id].discard(old)
                    succeeded += 1
                    break
                except RetryAfter as exc:
                    delay = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else float(exc.retry_after)
                    await asyncio.sleep(delay + 0.2)
                except TelegramError:
                    await asyncio.sleep(0.2)
            else:
                failed += 1
            await asyncio.sleep(0.04)
        return succeeded, failed

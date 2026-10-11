from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import or_, select
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from .db import transaction
from .models import Announcement, AnnouncementDelivery, NotificationPreference, User


async def publish_announcement(application, announcement_id: int) -> tuple[int, int]:
    """Deliver once per user and pin important messages without duplicating a sent message."""
    lock = application.bot_data.setdefault("announcement_publish_lock", asyncio.Lock())
    async with lock:
        return await _publish_announcement(application, announcement_id)


async def _publish_announcement(application, announcement_id: int) -> tuple[int, int]:
    factory = application.bot_data["session_factory"]
    with transaction(factory) as session:
        announcement = session.get(Announcement, announcement_id)
        if not announcement:
            raise LookupError("announcement not found")
        first_publish = announcement.published_at is None
        announcement.published_at = announcement.published_at or datetime.now(UTC)
        important = announcement.important
        if not first_publish:
            users = []
        elif important:
            users = session.scalars(select(User).where(User.is_active)).all()
        else:
            users = session.scalars(select(User).join(NotificationPreference).where(
                User.is_active, NotificationPreference.announcements_enabled)).all()
        if not important and application.bot_data.get("enforce_premium"):
            accounts = application.bot_data["accounts"]
            users = [user for user in users if accounts.has_premium_access(session, user.telegram_id)]
        for user in users:
            exists = session.scalar(select(AnnouncementDelivery.id).where(
                AnnouncementDelivery.announcement_id == announcement.id,
                AnnouncementDelivery.user_id == user.id,
            ))
            if not exists:
                session.add(AnnouncementDelivery(announcement_id=announcement.id, user_id=user.id))
        content = announcement.content

    succeeded = failed = 0
    while True:
        with transaction(factory) as session:
            pending = session.execute(
                select(AnnouncementDelivery, User).join(User).where(
                    AnnouncementDelivery.announcement_id == announcement_id,
                    (or_(AnnouncementDelivery.delivered_at.is_(None), AnnouncementDelivery.pinned_at.is_(None))
                     if important else AnnouncementDelivery.delivered_at.is_(None)),
                    AnnouncementDelivery.attempts < 5,
                    (AnnouncementDelivery.unpinned_at.is_(None) if important else True),
                ).limit(25)
            ).all()
        if not pending:
            break
        for delivery, user in pending:
            if not important and application.bot_data.get("enforce_premium"):
                with transaction(factory) as session:
                    if not application.bot_data["accounts"].has_premium_access(session, user.telegram_id):
                        session.get(AnnouncementDelivery, delivery.id).attempts = 5
                        continue
            error = None
            try:
                if delivery.delivered_at is None:
                    sent = await _telegram_call(application.bot.send_message, user.telegram_id,
                                                f"{'📌 重要公告' if important else '📢 公告'}\n\n{content}")
                    with transaction(factory) as session:
                        current = session.get(AnnouncementDelivery, delivery.id)
                        current.delivered_at = datetime.now(UTC)
                        current.message_id = sent.message_id if important else None
                if important:
                    message_id = delivery.message_id
                    if message_id is None:
                        with transaction(factory) as session:
                            message_id = session.get(AnnouncementDelivery, delivery.id).message_id
                    with transaction(factory) as session:
                        newer = session.scalar(select(AnnouncementDelivery.id).join(Announcement).where(
                            AnnouncementDelivery.user_id == user.id,
                            AnnouncementDelivery.announcement_id > announcement_id,
                            Announcement.important,
                            AnnouncementDelivery.pin_applied_at.is_not(None),
                            AnnouncementDelivery.unpinned_at.is_(None)).limit(1))
                    if newer is not None:
                        if delivery.pin_applied_at is not None:
                            await _telegram_call(application.bot.unpin_chat_message,
                                                 chat_id=user.telegram_id, message_id=message_id)
                        with transaction(factory) as session:
                            session.get(AnnouncementDelivery, delivery.id).unpinned_at = datetime.now(UTC)
                        continue
                    await _telegram_call(application.bot.pin_chat_message,
                                         chat_id=user.telegram_id, message_id=message_id)
                    with transaction(factory) as session:
                        session.get(AnnouncementDelivery, delivery.id).pin_applied_at = datetime.now(UTC)
                    with transaction(factory) as session:
                        old_pins = list(session.scalars(
                            select(AnnouncementDelivery).join(Announcement).where(
                                AnnouncementDelivery.user_id == user.id,
                                AnnouncementDelivery.id != delivery.id,
                                Announcement.important,
                                AnnouncementDelivery.pin_applied_at.is_not(None),
                                AnnouncementDelivery.unpinned_at.is_(None))))
                    for old in old_pins:
                        await _telegram_call(application.bot.unpin_chat_message,
                                             chat_id=user.telegram_id, message_id=old.message_id)
                        with transaction(factory) as session:
                            session.get(AnnouncementDelivery, old.id).unpinned_at = datetime.now(UTC)
                    with transaction(factory) as session:
                        session.get(AnnouncementDelivery, delivery.id).pinned_at = datetime.now(UTC)
                succeeded += 1
            except (Forbidden, BadRequest, TelegramError) as exc:
                error = f"{type(exc).__name__}: {exc}"[:1000]
                failed += 1
            with transaction(factory) as session:
                current = session.get(AnnouncementDelivery, delivery.id)
                current.attempts += 1
                current.last_error = error
            await asyncio.sleep(0.04)
    return succeeded, failed


async def _telegram_call(method, *args, **kwargs):
    while True:
        try:
            return await method(*args, **kwargs)
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.2)

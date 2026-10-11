from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from .db import transaction
from .models import Announcement, AnnouncementDelivery, NotificationPreference, User


async def publish_announcement(application, announcement_id: int) -> tuple[int, int]:
    """Deliver once per user, recording every attempt; safe to resume after restart."""
    factory = application.bot_data["session_factory"]
    with transaction(factory) as session:
        announcement = session.get(Announcement, announcement_id)
        if not announcement:
            raise LookupError("announcement not found")
        announcement.published_at = announcement.published_at or datetime.now(UTC)
        users = session.scalars(
            select(User).join(NotificationPreference).where(
                User.is_active, NotificationPreference.announcements_enabled
            )
        ).all()
        if application.bot_data.get("enforce_premium"):
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
                    AnnouncementDelivery.delivered_at.is_(None),
                    AnnouncementDelivery.attempts < 5,
                ).limit(25)
            ).all()
        if not pending:
            break
        for delivery, user in pending:
            if application.bot_data.get("enforce_premium"):
                with transaction(factory) as session:
                    if not application.bot_data["accounts"].has_premium_access(session, user.telegram_id):
                        session.get(AnnouncementDelivery, delivery.id).attempts = 5
                        continue
            error = None
            try:
                while True:
                    try:
                        await application.bot.send_message(user.telegram_id, f"📢 公告\n\n{content}")
                        break
                    except RetryAfter as exc:
                        await asyncio.sleep(float(exc.retry_after) + 0.2)
                succeeded += 1
            except (Forbidden, BadRequest, TelegramError) as exc:
                error = f"{type(exc).__name__}: {exc}"[:1000]
                failed += 1
            with transaction(factory) as session:
                current = session.get(AnnouncementDelivery, delivery.id)
                current.attempts += 1
                current.last_error = error
                if error is None:
                    current.delivered_at = datetime.now(UTC)
            await asyncio.sleep(0.04)
    return succeeded, failed

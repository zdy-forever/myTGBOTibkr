from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from telegram.error import Forbidden

from quantbot.announcements import publish_announcement
from quantbot.db import transaction
from quantbot.models import Announcement, AnnouncementDelivery


async def test_announcement_records_success_without_duplicate_delivery(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 1, "+61400000001")
        item = Announcement(content="maintenance", created_by_telegram_id=999)
        session.add(item)
        session.flush()
        announcement_id = item.id
    application = SimpleNamespace(
        bot_data={"session_factory": factory},
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    assert await publish_announcement(application, announcement_id) == (1, 0)
    assert await publish_announcement(application, announcement_id) == (0, 0)
    application.bot.send_message.assert_awaited_once()
    with transaction(factory) as session:
        assert session.scalar(select(func.count()).select_from(AnnouncementDelivery)) == 1
        assert session.scalar(select(AnnouncementDelivery)).delivered_at is not None


async def test_telegram_failure_is_recorded_and_bounded(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 1, "+61400000001")
        item = Announcement(content="maintenance", created_by_telegram_id=999)
        session.add(item)
        session.flush()
        announcement_id = item.id
    application = SimpleNamespace(
        bot_data={"session_factory": factory},
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=Forbidden("blocked"))),
    )
    assert await publish_announcement(application, announcement_id) == (0, 5)
    with transaction(factory) as session:
        delivery = session.scalar(select(AnnouncementDelivery))
        assert delivery.attempts == 5
        assert "Forbidden" in delivery.last_error

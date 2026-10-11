import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from telegram.error import Forbidden, RetryAfter

from quantbot import bot
from quantbot.announcements import _telegram_call, pending_announcement_ids, publish_announcement
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


async def test_important_announcement_reaches_all_verified_and_replaces_only_previous_important(factory, accounts):
    from quantbot.models import NotificationPreference, User

    with transaction(factory) as session:
        first_user = accounts.register(session, 1, "+61400000001").user
        accounts.register(session, 2, "+61400000002")
        session.get(NotificationPreference, first_user.id).announcements_enabled = False
        first = Announcement(content="first", important=True, created_by_telegram_id=999)
        second = Announcement(content="second", important=True, created_by_telegram_id=999)
        session.add_all([first, second])
        session.flush()
        first_id, second_id = first.id, second.id
    next_message = iter(range(100, 104))

    async def send_message(*_args, **_kwargs):
        return SimpleNamespace(message_id=next(next_message))

    api = SimpleNamespace(send_message=AsyncMock(side_effect=send_message),
                          pin_chat_message=AsyncMock(), unpin_chat_message=AsyncMock())
    application = SimpleNamespace(bot_data={"session_factory": factory, "accounts": accounts,
                                            "enforce_premium": True}, bot=api)
    assert await publish_announcement(application, first_id) == (2, 0)
    assert await publish_announcement(application, second_id) == (2, 0)
    assert await publish_announcement(application, second_id) == (0, 0)
    assert api.send_message.await_count == 4
    assert api.pin_chat_message.await_count == 4
    assert api.unpin_chat_message.await_count == 2
    with transaction(factory) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 2
        old = list(session.scalars(select(AnnouncementDelivery).where(
            AnnouncementDelivery.announcement_id == first_id)))
        new = list(session.scalars(select(AnnouncementDelivery).where(
            AnnouncementDelivery.announcement_id == second_id)))
        assert all(row.unpinned_at is not None for row in old)
        assert all(row.pinned_at is not None and row.unpinned_at is None for row in new)


async def test_important_pin_failure_retries_without_resending(factory, accounts):
    from telegram.error import BadRequest

    with transaction(factory) as session:
        accounts.register(session, 1, "+61400000001")
        item = Announcement(content="urgent", important=True, created_by_telegram_id=999)
        session.add(item)
        session.flush()
        announcement_id = item.id
    api = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=777)),
                          pin_chat_message=AsyncMock(side_effect=[BadRequest("try again"), None]),
                          unpin_chat_message=AsyncMock())
    application = SimpleNamespace(bot_data={"session_factory": factory}, bot=api)
    assert await publish_announcement(application, announcement_id) == (1, 1)
    api.send_message.assert_awaited_once()
    assert api.pin_chat_message.await_count == 2
    with transaction(factory) as session:
        delivery = session.scalar(select(AnnouncementDelivery))
        assert delivery.message_id == 777 and delivery.pinned_at is not None
        assert delivery.attempts == 2


async def test_important_command_previews_confirms_and_rejects_stale_buttons(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 999, "+61400000999")
        accounts.register(session, 1, "+61400000001")

    class Message:
        def __init__(self, text):
            self.text = text
            self.replies = []

        async def reply_text(self, text, **kwargs):
            self.replies.append((text, kwargs))

    class Query:
        def __init__(self, data):
            self.data = data
            self.answers = []

        async def answer(self, *args, **kwargs):
            self.answers.append((args, kwargs))

        async def edit_message_text(self, text):
            self.text = text

    tasks = []
    app = SimpleNamespace(
        bot_data={"session_factory": factory, "accounts": accounts, "enforce_premium": True},
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
                            pin_chat_message=AsyncMock(), unpin_chat_message=AsyncMock()),
        create_task=lambda coroutine, **_kwargs: tasks.append(coroutine),
    )
    context = SimpleNamespace(application=app, user_data={})
    user = SimpleNamespace(id=999)
    await bot.announce_start(SimpleNamespace(message=Message("/important_announce"), effective_user=user), context)
    first = Message("first draft")
    await bot.announce_text(SimpleNamespace(message=first), context)
    old_button = first.replies[0][1]["reply_markup"].inline_keyboard[0][0].callback_data
    second = Message("final draft")
    await bot.announce_text(SimpleNamespace(message=second), context)
    new_button = second.replies[0][1]["reply_markup"].inline_keyboard[0][0].callback_data
    old_query = Query(old_button)
    assert await bot.announce_confirm(SimpleNamespace(callback_query=old_query, effective_user=user), context) == bot.ANNOUNCE_CONFIRM
    assert "已失效" in old_query.answers[0][0][0]
    with transaction(factory) as session:
        assert session.query(Announcement).count() == 0
    new_query = Query(new_button)
    await bot.announce_confirm(SimpleNamespace(callback_query=new_query, effective_user=user), context)
    with transaction(factory) as session:
        item = session.scalar(select(Announcement))
        assert item.important and item.content == "final draft"
    await tasks.pop()
    app.bot.pin_chat_message.assert_awaited()


@pytest.mark.parametrize("duration_mode", ["false", "true"])
async def test_retry_after_supports_seconds_and_timedelta(monkeypatch, duration_mode):
    monkeypatch.setenv("PTB_TIMEDELTA", duration_mode)
    method = AsyncMock(side_effect=[RetryAfter(timedelta(seconds=2)), "ok"])
    with patch("quantbot.announcements.asyncio.sleep", new=AsyncMock()) as sleep:
        assert await _telegram_call(method) == "ok"
        sleep.assert_awaited_once_with(2.2)
    assert method.await_count == 2


@pytest.mark.parametrize("phase", ["before_publish", "send", "pin"])
async def test_recovery_after_restart_resumes_confirmed_announcement(factory, accounts, phase):
    with transaction(factory) as session:
        accounts.register(session, 1, "+61400000001")
        item = Announcement(content="confirmed", important=True, created_by_telegram_id=999)
        session.add(item)
        session.flush()
        aid = item.id
    api = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=777)),
                          pin_chat_message=AsyncMock(), unpin_chat_message=AsyncMock())
    if phase != "before_publish":
        getattr(api, "send_message" if phase == "send" else "pin_chat_message").side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await publish_announcement(SimpleNamespace(bot_data={"session_factory": factory}, bot=api), aid)
    assert pending_announcement_ids(factory) == [aid]
    # Recreate in-memory state as on a process restart, retaining only the database.
    fresh_api = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=778)),
                                pin_chat_message=AsyncMock(), unpin_chat_message=AsyncMock())
    app = SimpleNamespace(bot_data={"session_factory": factory}, bot=fresh_api)
    await bot.resume_announcements(SimpleNamespace(application=app))
    user_sends = [call for call in fresh_api.send_message.call_args_list if call.args[0] == 1]
    assert len(user_sends) == (0 if phase == "pin" else 1)
    fresh_api.pin_chat_message.assert_awaited_once_with(chat_id=1, message_id=777 if phase == "pin" else 778)
    assert pending_announcement_ids(factory) == []
    calls = fresh_api.send_message.await_count
    await bot.resume_announcements(SimpleNamespace(application=app))
    assert fresh_api.send_message.await_count == calls


async def test_recovery_ignores_finished_and_exhausted_deliveries(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 1, "+61400000001")
        item = Announcement(content="confirmed", created_by_telegram_id=999)
        session.add(item)
        session.flush()
        aid = item.id
    api = SimpleNamespace(send_message=AsyncMock(side_effect=Forbidden("blocked")))
    app = SimpleNamespace(bot_data={"session_factory": factory}, bot=api)
    await publish_announcement(app, aid)
    assert pending_announcement_ids(factory) == []
    await bot.resume_announcements(SimpleNamespace(application=app))
    assert api.send_message.await_count == 5

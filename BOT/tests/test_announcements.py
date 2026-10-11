from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import Application

from quantbot import bot
from quantbot.announcements import publish_announcement
from quantbot.db import transaction
from quantbot.models import NotificationPreference, PhoneWhitelist


def app_for(factory, **overrides):
    api = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=777)),
                          pin_chat_message=AsyncMock(), unpin_chat_message=AsyncMock())
    for key, value in overrides.items():
        setattr(api, key, value)
    return SimpleNamespace(bot_data={"session_factory": factory}, bot=api)


async def test_announcements_do_not_write_database_or_add_number_tags(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 1, "+61400000001")
    app = app_for(factory)
    assert await publish_announcement(app, "正文") == (1, 0)
    app.bot.send_message.assert_awaited_once_with(1, "📢 公告\n\n正文")
    with transaction(factory) as session:
        assert "announcements" not in inspect(session.bind).get_table_names()
        assert "announcement_deliveries" not in inspect(session.bind).get_table_names()
    await bot._publish_announcement_job(app, "正文", 999)
    assert "公告处理完成" in app.bot.send_message.call_args.args[1]
    assert "#" not in app.bot.send_message.call_args.args[1]
    assert "正文" not in repr(app.bot_data)


async def test_announcement_failure_bounded(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 1, "+61400000001")
    app = app_for(factory, send_message=AsyncMock(side_effect=Forbidden("blocked")))
    assert await publish_announcement(app, "正文") == (0, 1)
    assert app.bot.send_message.await_count == 5


async def test_important_replaces_only_known_pins_and_includes_nonpremium(factory, accounts):
    with transaction(factory) as session:
        user = accounts.register(session, 1, "+61400000001").user
        session.get(NotificationPreference, user.id).announcements_enabled = False
    app = app_for(factory, send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=100),
                                                               SimpleNamespace(message_id=101)]))
    app.bot_data.update(accounts=accounts, enforce_premium=True)
    assert await publish_announcement(app, "ordinary") == (0, 0)
    assert await publish_announcement(app, "first", important=True) == (1, 0)
    assert await publish_announcement(app, "second", important=True) == (1, 0)
    app.bot.unpin_chat_message.assert_awaited_once_with(chat_id=1, message_id=100)
    assert app.bot_data["important_announcement_pins"] == {1: {101}}
    # Restart loses pin tracking and never removes unrelated/pre-restart pins.
    fresh = app_for(factory)
    await publish_announcement(fresh, "third", important=True)
    fresh.bot.unpin_chat_message.assert_not_awaited()


async def test_pin_failure_retries_without_resending(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 1, "+61400000001")
    app = app_for(factory, pin_chat_message=AsyncMock(side_effect=[BadRequest("retry"), None]))
    assert await publish_announcement(app, "urgent", important=True) == (1, 0)
    app.bot.send_message.assert_awaited_once()
    assert app.bot.pin_chat_message.await_count == 2


@pytest.mark.parametrize("duration_mode", ["false", "true"])
async def test_retry_after_supports_both_formats(factory, accounts, monkeypatch, duration_mode):
    monkeypatch.setenv("PTB_TIMEDELTA", duration_mode)
    with transaction(factory) as session:
        accounts.register(session, 1, "+61400000001")
    app = app_for(factory, send_message=AsyncMock(side_effect=[RetryAfter(timedelta(0)),
                                                               SimpleNamespace(message_id=777)]))
    assert await publish_announcement(app, "正文") == (1, 0)
    assert app.bot.send_message.await_count == 2


async def test_normal_announcement_requires_enabled_notification_and_premium(factory, accounts):
    with transaction(factory) as session:
        user = accounts.register(session, 1, "+61400000001").user
        accounts.register(session, 2, "+61400000002")
        session.add(PhoneWhitelist(phone_fingerprint=user.phone_fingerprint, created_by_telegram_id=999))
    app = app_for(factory)
    app.bot_data.update(accounts=accounts, enforce_premium=True)
    assert await publish_announcement(app, "正文") == (1, 0)
    app.bot.send_message.assert_awaited_once_with(1, "📢 公告\n\n正文")


def test_no_recovery_job_registered(factory, accounts):
    app = bot.configure_application(Application.builder().token("123456:TEST").build(), accounts, factory)
    assert "announcement-recovery" not in {job.name for job in app.job_queue.jobs()}


async def test_unpin_failure_does_not_resend_new_announcement(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 1, "+61400000001")
    app = app_for(factory, unpin_chat_message=AsyncMock(side_effect=[BadRequest("retry"), None]))
    app.bot_data["important_announcement_pins"] = {1: {100}}
    assert await publish_announcement(app, "new", important=True) == (1, 0)
    app.bot.send_message.assert_awaited_once()
    app.bot.pin_chat_message.assert_awaited_once()
    assert app.bot.unpin_chat_message.await_count == 2
    assert app.bot_data["important_announcement_pins"] == {1: {777}}


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
    assert not tasks
    new_query = Query(new_button)
    await bot.announce_confirm(SimpleNamespace(callback_query=new_query, effective_user=user), context)
    assert len(tasks) == 1
    assert "已保存" not in new_query.text
    await tasks.pop()
    app.bot.pin_chat_message.assert_awaited()

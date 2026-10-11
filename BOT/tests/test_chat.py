from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from telegram.error import Forbidden
from telegram.ext import ConversationHandler

from quantbot import bot
from quantbot.chat import (
    MAX_ATTEMPTS,
    create_message,
    deliver_chat_messages,
    purge_expired_chat_messages,
    reply_callback_data,
)
from quantbot.models import AdminGrant, AdminPhoneGrant, ChatDelivery, ChatMessage, PhoneWhitelist
from tests.test_strategy_service import add_user


class Message:
    def __init__(self, text=None):
        self.text, self.texts = text, []

    async def reply_text(self, text, **kwargs):
        self.texts.append((text, kwargs))


class Query:
    def __init__(self, data):
        self.data, self.answers, self.edits = data, [], []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class Telegram:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_for:
            raise Forbidden("blocked")
        self.sent.append((chat_id, text, kwargs))


class App:
    def __init__(self, factory, accounts, telegram=None):
        self.bot = telegram or Telegram()
        self.bot_data = {"session_factory": factory, "accounts": accounts}


def command(factory, accounts, telegram_id, *, text=None, app=None, user_data=None):
    message = Message(text)
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=telegram_id),
                             effective_chat=SimpleNamespace(id=telegram_id))
    context = SimpleNamespace(application=app or App(factory, accounts), args=[], user_data=user_data or {})
    return update, context, message


def callback(factory, accounts, telegram_id, data, *, app=None, user_data=None):
    query = Query(data)
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=telegram_id))
    context = SimpleNamespace(application=app or App(factory, accounts), args=[], user_data=user_data or {})
    return update, context, query


async def test_bound_user_can_leave_persisted_message_and_admin_can_reply_after_restart(factory, accounts):
    uid = add_user(factory, 1)
    add_user(factory, 999)
    app = App(factory, accounts)
    update, context, response = command(factory, accounts, 1, app=app)
    assert await bot.chat_start(update, context) == bot.CHAT_TEXT
    update.message.text = "请看 <b>这个</b> & 谢谢"
    assert await bot.chat_text(update, context) == ConversationHandler.END
    with factory.begin() as session:
        message = session.scalar(select(ChatMessage))
        delivery = session.scalar(select(ChatDelivery))
        assert message.user_id == uid and message.body == "请看 <b>这个</b> & 谢谢"
        assert delivery.delivered_at is not None and delivery.attempts == 1
        message_id = message.id
        callback_data = reply_callback_data(message)
    assert "保存 14 天" in response.texts[-1][0]
    assert app.bot.sent[0][0] == 999
    assert "&lt;b&gt;这个&lt;/b&gt; &amp; 谢谢" in app.bot.sent[0][1]
    assert app.bot.sent[0][2]["reply_markup"].inline_keyboard[0][0].callback_data == callback_data

    # A new application simulates a restart; the database is the source of truth.
    restarted = App(factory, accounts)
    update, context, query = callback(
        factory, accounts, 999, callback_data, app=restarted,
        user_data={"pending_command_args": ("portfolio", datetime.now(UTC) + timedelta(minutes=5))},
    )
    assert await bot.chat_reply_start(update, context) == bot.CHAT_REPLY_TEXT
    assert "pending_command_args" not in context.user_data
    assert context.user_data["chat_reply_id"] == message_id
    reply_update, reply_context, reply_message = command(
        factory, accounts, 999, text="管理员回复内容", app=restarted, user_data=context.user_data)
    assert await bot.chat_reply_text(reply_update, reply_context) == ConversationHandler.END
    assert restarted.bot.sent == [(1, f"💬 管理员回复你的留言 #{message_id}\n\n管理员回复内容", {})]
    with factory.begin() as session:
        stored = session.get(ChatMessage, message_id)
        assert stored.reply_body == "管理员回复内容" and stored.replied_by_telegram_id == 999 and stored.replied_at
    assert "已回复" in reply_message.texts[-1][0]


@pytest.mark.parametrize(
    ("whitelist_label", "admin_label", "phone_admin_label", "expected"),
    [
        (None, None, None, None),
        ("白名单备注", None, None, "备注：(白名单备注)"),
        (None, "管理员备注", None, "备注：(管理员备注)"),
        (None, None, "手机号管理员备注", "备注：(手机号管理员备注)"),
        ("白名单<&>", "管理员<&>", None, "备注：(白名单&lt;&amp;&gt;/管理员&lt;&amp;&gt;)"),
    ],
)
async def test_admin_receives_sender_notes(factory, accounts, whitelist_label, admin_label, phone_admin_label, expected):
    user_id = add_user(factory, 1)
    with factory.begin() as session:
        if whitelist_label is not None:
            session.add(PhoneWhitelist(phone_fingerprint="fp1", label=whitelist_label, created_by_telegram_id=999))
        if admin_label is not None:
            session.add(AdminGrant(telegram_id=1, label=admin_label, created_by_telegram_id=999))
        if phone_admin_label is not None:
            session.add(AdminPhoneGrant(phone_fingerprint="fp1", telegram_id=1,
                                        label=phone_admin_label, created_by_telegram_id=999))
        create_message(session, user_id, "留言内容", frozenset({999}))
    app = App(factory, accounts)
    assert await deliver_chat_messages(app) == (1, 0)
    notification = app.bot.sent[0][1]
    if expected is None:
        assert "备注：" not in notification
    else:
        assert expected in notification
    assert "留言内容" in notification


async def test_unclaimed_admin_phone_note_is_not_shown(factory, accounts):
    user_id = add_user(factory, 1)
    with factory.begin() as session:
        session.add(AdminPhoneGrant(phone_fingerprint="fp1", telegram_id=2,
                                    label="别人的备注", created_by_telegram_id=999))
        create_message(session, user_id, "留言内容", frozenset({999}))
    app = App(factory, accounts)
    assert await deliver_chat_messages(app) == (1, 0)
    assert "备注：" not in app.bot.sent[0][1]


async def test_chat_requires_binding_and_configured_admin(factory, accounts):
    update, context, message = command(factory, accounts, 1)
    assert await bot.chat_start(update, context) == ConversationHandler.END
    assert "/start" in message.texts[-1][0]
    add_user(factory, 1)
    empty_accounts = type(accounts)(accounts.protector, frozenset())
    update, context, message = command(factory, empty_accounts, 1)
    assert await bot.chat_start(update, context) == ConversationHandler.END
    assert "暂未配置" in message.texts[-1][0]
    with factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 0


async def test_chat_validates_empty_and_oversized_text(factory, accounts):
    add_user(factory, 1)
    app = App(factory, accounts)
    for value in ("   ", "x" * 2001):
        update, context, message = command(factory, accounts, 1, text=value, app=app)
        assert await bot.chat_text(update, context) == bot.CHAT_TEXT
        assert message.texts
    with factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 0


async def test_failed_admin_delivery_is_persisted_retried_and_capped(factory, accounts):
    add_user(factory, 1)
    failing = Telegram(fail_for={999})
    app = App(factory, accounts, failing)
    update, context, response = command(factory, accounts, 1, text="hello", app=app)
    await bot.chat_text(update, context)
    assert "已保存 14 天" in response.texts[-1][0] and "自动重试" in response.texts[-1][0]
    for _ in range(MAX_ATTEMPTS + 2):
        await deliver_chat_messages(app)
    with factory.begin() as session:
        delivery = session.scalar(select(ChatDelivery))
        assert delivery.attempts == MAX_ATTEMPTS and delivery.delivered_at is None and "Forbidden" in delivery.last_error


def test_messages_and_replies_are_deleted_at_14_days_with_deliveries(factory, accounts):
    old_uid = add_user(factory, 1)
    fresh_uid = add_user(factory, 2)
    now = datetime(2026, 10, 20, tzinfo=UTC)
    with factory.begin() as session:
        old = ChatMessage(user_id=old_uid, body="old", reply_body="old reply",
                          replied_at=now - timedelta(days=13), created_at=now - timedelta(days=14))
        fresh = ChatMessage(user_id=fresh_uid, body="fresh", created_at=now - timedelta(days=14) + timedelta(seconds=1))
        session.add_all([old, fresh])
        session.flush()
        session.add_all([ChatDelivery(message_id=old.id, maintainer_telegram_id=999),
                         ChatDelivery(message_id=fresh.id, maintainer_telegram_id=999)])
        old_id, fresh_id = old.id, fresh.id
    assert purge_expired_chat_messages(factory, now) == 1
    with factory.begin() as session:
        assert session.get(ChatMessage, old_id) is None and session.get(ChatDelivery, old_id) is None
        assert session.get(ChatMessage, fresh_id) is not None
        assert session.scalar(select(func.count()).select_from(ChatDelivery)) == 1


async def test_reply_is_owner_checked_and_only_sent_once(factory, accounts):
    uid = add_user(factory, 1)
    add_user(factory, 999)
    with factory.begin() as session:
        message = ChatMessage(user_id=uid, body="hello")
        session.add(message)
        session.flush()
        message_id = message.id
        callback_data = reply_callback_data(message)
    update, context, query = callback(factory, accounts, 1, callback_data)
    assert await bot.chat_reply_start(update, context) == ConversationHandler.END
    assert query.answers[0][0] == "仅管理员可以回复。"
    app = App(factory, accounts)
    update, context, _ = callback(factory, accounts, 999, callback_data, app=app)
    await bot.chat_reply_start(update, context)
    reply, reply_context, _ = command(factory, accounts, 999, text="once", app=app, user_data=context.user_data)
    await bot.chat_reply_text(reply, reply_context)
    update2, context2, query2 = callback(factory, accounts, 999, callback_data, app=app)
    assert await bot.chat_reply_start(update2, context2) == ConversationHandler.END
    assert "已经回复" in query2.edits[0][0]
    assert len([item for item in app.bot.sent if item[0] == 1]) == 1


async def test_expired_message_cannot_be_replied_before_scheduled_cleanup_runs(factory, accounts):
    uid = add_user(factory, 1)
    add_user(factory, 999)
    with factory.begin() as session:
        message = ChatMessage(user_id=uid, body="expired", created_at=datetime.now(UTC) - timedelta(days=15))
        session.add(message)
        session.flush()
        message_id = message.id
        callback_data = reply_callback_data(message)
    update, context, query = callback(factory, accounts, 999, callback_data)
    assert await bot.chat_reply_start(update, context) == ConversationHandler.END
    assert "超过 14 天" in query.edits[0][0]
    with factory.begin() as session:
        assert session.get(ChatMessage, message_id) is None


async def test_expired_reply_button_cannot_target_reused_message_id(factory, accounts):
    old_user = add_user(factory, 1)
    new_user = add_user(factory, 2)
    add_user(factory, 999)
    with factory.begin() as session:
        old = ChatMessage(user_id=old_user, body="old", created_at=datetime.now(UTC) - timedelta(days=15))
        session.add(old)
        session.flush()
        old_callback = reply_callback_data(old)
        old_id = old.id
    purge_expired_chat_messages(factory)
    with factory.begin() as session:
        fresh = ChatMessage(user_id=new_user, body="fresh")
        session.add(fresh)
        session.flush()
        assert fresh.id == old_id
        fresh_callback = reply_callback_data(fresh)
    update, context, query = callback(factory, accounts, 999, old_callback)
    assert await bot.chat_reply_start(update, context) == ConversationHandler.END
    assert "已不存在" in query.edits[0][0]
    assert "chat_reply_id" not in context.user_data
    update, context, _ = callback(factory, accounts, 999, fresh_callback)
    assert await bot.chat_reply_start(update, context) == bot.CHAT_REPLY_TEXT


def test_chat_maintenance_job_is_registered(factory, accounts):
    from telegram.ext import Application
    from quantbot.chat import maintain_chat_messages

    app = bot.configure_application(Application.builder().token("123456:TEST").build(), accounts, factory)
    jobs = {job.name: job for job in app.job_queue.jobs()}
    assert jobs["chat-message-maintenance"].callback is maintain_chat_messages

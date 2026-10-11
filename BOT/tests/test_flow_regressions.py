from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram import Chat, Message, MessageEntity, Update, User
from telegram.error import RetryAfter
from telegram.ext import Application, ApplicationHandlerStop, ExtBot

from quantbot import bot
from quantbot import strategy_service as svc
from quantbot.models import CashAccount, NotificationPreference, PremiumSubscription, UserStrategy
from tests.test_premium_access import Message as StubMessage
from tests.test_premium_access import context, make_update
from tests.test_strategy_service import SETTINGS, add_user, make_run


async def test_cancel_after_switching_input_flow_cannot_change_cash(factory, accounts):
    uid = add_user(factory, 999, cash="200")
    api = ExtBot("123456:TEST")
    api._bot_user = User(555, "testbot", True, username="testbot")
    app = bot.configure_application(Application.builder().bot(api).build(), accounts, factory)
    with (patch.object(ExtBot, "initialize", new=AsyncMock()),
          patch.object(ExtBot, "shutdown", new=AsyncMock()),
          patch.object(ExtBot, "send_message", new=AsyncMock())):
        await app.initialize()
        try:
            for index, text in enumerate(["/add", "/cash", "/cancel", "123 USD"], 1):
                entities = [MessageEntity("bot_command", 0, len(text))] if text.startswith("/") else []
                message = Message(index, datetime.now(UTC), Chat(999, "private"),
                                  from_user=User(999, "test", False), text=text, entities=entities)
                message.set_bot(api)
                update = Update(index, message=message)
                await app.process_update(update)
            with factory.begin() as session:
                assert session.get(CashAccount, uid).balance == Decimal(200)
        finally:
            await app.shutdown()


async def test_admin_access_without_strategy_includes_followups_but_not_strategy_commands(factory, accounts):
    uid = add_user(factory, 999)
    with factory.begin() as session:
        session.delete(session.get(UserStrategy, (uid, "strategy1")))
    ctx = context(factory, accounts)
    ctx.user_data = {}
    for command in ("/admin_help", "/whitelist_add", "/important_announce"):
        await bot.authorization_gate(make_update(StubMessage(text=command), user_id=999), ctx)
    ctx.user_data["pending_command_args"] = ("whitelist_add", datetime.now(UTC) + timedelta(minutes=5))
    await bot.authorization_gate(make_update(StubMessage(text="phone details"), user_id=999), ctx)
    ctx.user_data.clear()
    ctx.user_data["announcement_important"] = False
    await bot.authorization_gate(make_update(StubMessage(text="公告正文"), user_id=999), ctx)
    for data in ("announce:yes:token", "chat-reply:1:token"):
        await bot.authorization_gate(make_update(user_id=999, callback=SimpleNamespace(data=data)), ctx)
    ctx.user_data.clear()
    ctx.user_data["chat_reply_id"] = 1
    await bot.authorization_gate(make_update(StubMessage(text="管理员回复"), user_id=999), ctx)
    for command in ("/check", "/auto_notice_date"):
        with pytest.raises(ApplicationHandlerStop):
            await bot.authorization_gate(make_update(StubMessage(text=command), user_id=999), ctx)
    with factory.begin() as session:
        status = bot._ui_status(session, accounts, 999)
    commands = {item.command for item in bot._menu_for(status)}
    assert "admin_help" in commands and "check" not in commands


@pytest.mark.parametrize("change", ["strategy", "preferences", "subscription", "unbind", "none"])
async def test_rate_limit_retry_rechecks_current_access(factory, accounts, change):
    uid = add_user(factory, 1, cash="200")
    with factory.begin() as session:
        session.add(PremiumSubscription(user_id=uid, expires_at=datetime.now(UTC) + timedelta(days=1)))
        run, _ = svc.store_run(session, make_run())
        run_id = run.id
    svc.generate_for_all_users(factory, run_id, SETTINGS)

    class RateLimitedBot:
        calls = 0
        sent = 0

        async def send_message(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                with factory.begin() as session:
                    if change == "strategy":
                        session.delete(session.get(UserStrategy, (uid, "strategy1")))
                    elif change == "preferences":
                        session.get(NotificationPreference, uid).signals_enabled = False
                    elif change == "subscription":
                        session.get(PremiumSubscription, uid).expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    elif change == "unbind":
                        accounts.unregister(session, 1)
                raise RetryAfter(0)
            self.sent += 1

    api = RateLimitedBot()
    app = SimpleNamespace(bot=api, bot_data={"session_factory": factory, "accounts": accounts, "enforce_premium": True})
    assert await svc.deliver_pending(app) == ((1, 0) if change == "none" else (0, 0))
    assert api.sent == (1 if change == "none" else 0)
    assert api.calls == (2 if change == "none" else 1)

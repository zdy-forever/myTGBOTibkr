from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.ext import Application, ApplicationHandlerStop, ConversationHandler, TypeHandler

from quantbot import bot
from quantbot.db import transaction
from quantbot.models import CashAccount, Position, Transaction, UserStrategy


class Message:
    def __init__(self, *, text=None, contact=None, user_id=101, forwarded=False, payment=None):
        self.text = text
        self.contact = contact
        self.from_user = SimpleNamespace(id=user_id)
        self.forward_origin = object() if forwarded else None
        self.successful_payment = payment
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def make_update(message=None, *, user_id=101, chat_type="private", callback=None, checkout=None):
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, type=chat_type) if chat_type else None,
        callback_query=callback,
        pre_checkout_query=checkout,
    )


def context(factory, accounts, *, bot_api=None):
    app = SimpleNamespace(bot_data={"session_factory": factory, "accounts": accounts})
    return SimpleNamespace(application=app, bot=bot_api, args=[])


async def test_unverified_start_and_contact_evidence(factory, accounts):
    ctx = context(factory, accounts)
    first = Message(text="/start")
    assert await bot.start(make_update(first), ctx) == bot.CONTACT
    assert "分享我的手机号" in first.replies[0][0]
    assert first.replies[0][1]["reply_markup"].keyboard[0][0].request_contact


    typed = Message(text="+61 400 000 001")
    with pytest.raises(ApplicationHandlerStop):
        await bot.authorization_gate(make_update(typed), ctx)
    with transaction(factory) as session:
        assert not accounts.is_registered(session, 101)
        assert session.query(CashAccount).count() == 0

    for contact_id, forwarded, phone, private in [
        (102, False, "+61400000001", True),
        (101, True, "+61400000001", True),
        (101, False, "12345", True),
        (101, False, "+61400000001", False),
    ]:
        message = Message(contact=SimpleNamespace(user_id=contact_id, phone_number=phone), forwarded=forwarded)
        await bot.contact(make_update(message, chat_type="private" if private else "group"), ctx)
        with transaction(factory) as session:
            assert not accounts.is_registered(session, 101)

    again = Message(text="/start")
    assert await bot.start(make_update(again), ctx) == bot.CONTACT
    valid = Message(contact=SimpleNamespace(user_id=101, phone_number="+61 400 000 001"))
    assert await bot.contact(make_update(valid), ctx) == ConversationHandler.END
    assert "需要开通订阅" in valid.replies[0][0]
    with transaction(factory) as session:
        user = accounts.by_telegram(session, 101)
        assert user.phone_encrypted is None
        assert accounts.is_registered(session, 101)
        assert not accounts.has_premium_access(session, 101)


async def test_pinned_service_message_does_not_trigger_registration_reply(factory, accounts):
    pinned = Message(text=None)
    pinned.pinned_message = Message(text="important")
    with pytest.raises(ApplicationHandlerStop):
        await bot.authorization_gate(make_update(pinned), context(factory, accounts))
    assert not pinned.replies
    bot_origin = Message(text=None)
    bot_origin.from_user.is_bot = True
    with pytest.raises(ApplicationHandlerStop):
        await bot.authorization_gate(make_update(bot_origin), context(factory, accounts))
    assert not bot_origin.replies


async def test_admin_reply_callback_on_bot_message_passes_authorization_gate(factory, accounts):
    with transaction(factory) as session:
        admin = accounts.register(session, 999, "+61400000999").user
        session.add(UserStrategy(user_id=admin.id, strategy_id="strategy1"))
    bot_message = Message(text="💬 用户留言", user_id=999)
    bot_message.from_user.is_bot = True
    callback = SimpleNamespace(data="chat-reply:1:20261011123456789012")
    update = make_update(user_id=999, callback=callback)
    update.effective_message = bot_message
    await bot.authorization_gate(update, context(factory, accounts))

    # The same bot-authored notification must not bypass the registration gate
    # when an unverified account presses its button.
    update.effective_user.id = 101
    callback.answers = []

    async def answer(text, **kwargs):
        callback.answers.append(text)

    callback.answer = answer
    with pytest.raises(ApplicationHandlerStop):
        await bot.authorization_gate(update, context(factory, accounts))
    assert "验证手机号" in callback.answers[0]


async def test_unregistered_user_can_send_help_topic_as_followup(factory, accounts):
    ctx = context(factory, accounts)
    ctx.user_data = {}
    command = Message(text="/help")
    await bot._staged_command_entry(make_update(command), ctx)
    topic = Message(text="导入")
    await bot.authorization_gate(make_update(topic), ctx)
    await bot._staged_command_data(make_update(topic), ctx)
    assert topic.replies and "CSV" in topic.replies[0][0]


async def test_whitelist_immediately_activates_and_keeps_existing_data(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 999, "+61400000999")
        accounts.whitelist_add(session, 999, "+61 400 000 001")
    ctx = context(factory, accounts)
    phone = Message(contact=SimpleNamespace(user_id=101, phone_number="+61400000001"))
    await bot.contact(make_update(phone), ctx)
    assert "白名单 Premium 已激活" in phone.replies[0][0]
    assert "/strategy1" in phone.replies[0][0]
    with transaction(factory) as session:
        user = accounts.by_telegram(session, 101)
        session.get(CashAccount, user.id).balance = Decimal(500)
        session.add(Position(user_id=user.id, symbol="AAPL", exchange="US", shares=Decimal(2),
                             average_cost=Decimal(100), currency="USD"))
        user_id = user.id
    start = Message(text="/start")
    assert await bot.start(make_update(start), ctx) == ConversationHandler.END
    assert "白名单 Premium 已激活" in start.replies[0][0]
    assert "/strategy1" in start.replies[0][0]
    with transaction(factory) as session:
        user = accounts.by_telegram(session, 101)
        assert user.id == user_id
        assert session.get(CashAccount, user.id).balance == Decimal(500)
        assert session.query(Position).filter_by(user_id=user.id).count() == 1
        assert session.query(Transaction).filter_by(user_id=user.id).count() == 0
        assert accounts.whitelist_remove(session, 999, "+61 400 000 001")
        assert not accounts.has_premium_access(session, 101)


async def test_gate_blocks_commands_callbacks_and_payments_before_verification(factory, accounts):
    ctx = context(factory, accounts)
    for command in ("/portfolio", "/import", "/check", "/cash", "/subscribe", "/whitelist_add"):
        message = Message(text=command)
        with pytest.raises(ApplicationHandlerStop):
            await bot.authorization_gate(make_update(message), ctx)
        assert "/start" in message.replies[0][0]
    callback = SimpleNamespace(data="remove:yes", answers=[])

    async def answer(text, **kwargs):
        callback.answers.append(text)

    callback.answer = answer
    with pytest.raises(ApplicationHandlerStop):
        await bot.authorization_gate(make_update(callback=callback), ctx)
    assert "验证手机号" in callback.answers[0]

    checkout = SimpleNamespace(from_user=SimpleNamespace(id=101), answers=[])

    async def checkout_answer(**kwargs):
        checkout.answers.append(kwargs)

    checkout.answer = checkout_answer
    with pytest.raises(ApplicationHandlerStop):
        await bot.authorization_gate(make_update(chat_type=None, checkout=checkout), ctx)
    assert checkout.answers[0]["ok"] is False

    with transaction(factory) as session:
        accounts.register(session, 101, "+61400000001")
    for command in ("/portfolio", "/check", "/import"):
        message = Message(text=command)
        with pytest.raises(ApplicationHandlerStop):
            await bot.authorization_gate(make_update(message), ctx)
        assert "/subscribe" in message.replies[0][0]
    admin_only = Message(text="/runstrategy")
    with pytest.raises(ApplicationHandlerStop):
        await bot.authorization_gate(make_update(admin_only), ctx)
    assert "仅已验证的管理员" in admin_only.replies[0][0]
    await bot.authorization_gate(make_update(Message(text="/subscribe")), ctx)
    await bot.authorization_gate(make_update(Message(text="/chat")), ctx)
    ctx.user_data = {"chat_allowed": True}
    await bot.authorization_gate(make_update(Message(text="给管理员留言")), ctx)
    ctx.user_data.clear()
    callback.answers.clear()
    with pytest.raises(ApplicationHandlerStop):
        await bot.authorization_gate(make_update(callback=callback), ctx)
    assert "Premium" in callback.answers[0]


async def test_strategy_selection_is_per_premium_user_and_strategy_two_stays_unavailable(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 999, "+61400000999")
        first = accounts.register(session, 101, "+61400000001").user
        second = accounts.register(session, 102, "+61400000002").user
        accounts.whitelist_add(session, 999, "+61 400 000 001")
        first_id, second_id = first.id, second.id
        session.add(UserStrategy(user_id=first_id, strategy_id="strategy_future"))

    ctx = context(factory, accounts)
    for command in ("/strategy", "/strategy1", "/strategy2"):
        message = Message(text=command, user_id=102)
        with pytest.raises(ApplicationHandlerStop):
            await bot.authorization_gate(make_update(message, user_id=102), ctx)
        assert "/subscribe" in message.replies[0][0]

    menu_message = Message(text="/strategy")
    await bot.authorization_gate(make_update(menu_message), ctx)
    await bot.strategy_command(make_update(menu_message), ctx)
    assert "/strategy1" in menu_message.replies[0][0]
    assert "月度定投个股(加强型 QQQ)" in menu_message.replies[0][0]
    assert "研发中" in menu_message.replies[0][0]

    select_message = Message(text="/strategy1")
    await bot.strategy_one(make_update(select_message), ctx)
    unavailable_message = Message(text="/strategy2")
    await bot.strategy_two(make_update(unavailable_message), ctx)
    assert "研发中" in unavailable_message.replies[0][0]
    with transaction(factory) as session:
        assert session.get(UserStrategy, (first_id, "strategy1")) is not None
        assert session.get(UserStrategy, (first_id, "strategy_future")) is not None
        assert session.get(UserStrategy, (second_id, "strategy1")) is None
    off_message = Message(text="/strategy1_off")
    await bot.strategy_one_off(make_update(off_message), ctx)
    with transaction(factory) as session:
        assert session.get(UserStrategy, (first_id, "strategy1")) is None
        assert session.get(UserStrategy, (first_id, "strategy_future")) is not None
    blocked_check = Message(text="/check")
    with pytest.raises(ApplicationHandlerStop):
        await bot.authorization_gate(make_update(blocked_check), ctx)
    assert "/strategy1" in blocked_check.replies[0][0]
    assert "strategy" in [item.command for item in bot._menu_for("whitelist")]
    assert "strategy" not in [item.command for item in bot._menu_for("subscription_required")]


async def test_premium_start_requires_strategy_then_shows_strategy_one_welcome(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 999, "+61400000999")
        user = accounts.register(session, 101, "+61400000001").user
        accounts.whitelist_add(session, 999, "+61 400 000 001")
        user_id = user.id
    ctx = context(factory, accounts)
    start_message = Message(text="/start")
    await bot.start(make_update(start_message), ctx)
    assert "Premium 已开通" in start_message.replies[0][0]
    assert "/strategy1" in start_message.replies[0][0]
    assert "/strategy1_off" not in start_message.replies[0][0]
    for status in ("strategy_required", "admin_strategy_required"):
        assert "strategy1_off" not in {item.command for item in bot._menu_for(status)}
    for handler, command in ((bot.strategy_command, "/strategy"), (bot.help_command, "/help")):
        message = Message(text=command)
        await handler(make_update(message), ctx)
        assert "/strategy1_off" not in message.replies[0][0]
    not_enabled = Message(text="/strategy1_off")
    await bot.strategy_one_off(make_update(not_enabled), ctx)
    assert "尚未启用" in not_enabled.replies[0][0]
    assert "先用 /add 或 /import" not in start_message.replies[0][0]
    for command in ("/check", "/auto_notice_date", "/portfolio"):
        blocked = Message(text=command)
        with pytest.raises(ApplicationHandlerStop):
            await bot.authorization_gate(make_update(blocked), ctx)
        assert "先启用" in blocked.replies[0][0]
    with transaction(factory) as session:
        assert session.get(UserStrategy, (user_id, "strategy1")) is None
    await bot.strategy_one(make_update(Message(text="/strategy1")), ctx)
    selected_start = Message(text="/start")
    await bot.start(make_update(selected_start), ctx)
    assert "策略 1：月度定投个股(加强型 QQQ)" in selected_start.replies[0][0]
    assert "先用 /add 或 /import" in selected_start.replies[0][0]
    assert "/strategy1_off" in selected_start.replies[0][0]
    assert "strategy1_off" in {item.command for item in bot._menu_for("whitelist")}
    selected_menu = Message(text="/strategy")
    await bot.strategy_command(make_update(selected_menu), ctx)
    assert "/strategy1_off" in selected_menu.replies[0][0]
    await bot.authorization_gate(make_update(Message(text="/check")), ctx)


async def test_premium_and_admin_commands_stay_private(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 999, "+61400000999")
        accounts.register(session, 101, "+61400000001")
        accounts.whitelist_add(session, 999, "+61400000001")
    ctx = context(factory, accounts)
    for telegram_id, command in ((999, "/portfolio"), (999, "/admin_list"), (101, "/portfolio")):
        message = Message(text=command, user_id=telegram_id)
        with pytest.raises(ApplicationHandlerStop):
            await bot.authorization_gate(make_update(message, user_id=telegram_id, chat_type="group"), ctx)
        assert "私聊" in message.replies[0][0]
        assert "持仓" not in message.replies[0][0]


async def test_old_unbind_confirmation_cannot_delete_recreated_account(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 101, "+61400000001")
    ctx = context(factory, accounts)
    ctx.user_data = {}
    start_message = Message(text="/unbind")
    await bot.unbind_start(make_update(start_message), ctx)
    old_button = start_message.replies[0][1]["reply_markup"].inline_keyboard[0][0].callback_data
    with transaction(factory) as session:
        accounts.unregister(session, 101)
        replacement = accounts.register(session, 101, "+61400000001").user
        session.get(CashAccount, replacement.id).balance = Decimal(500)

    class OldButton:
        data = old_button

        def __init__(self):
            self.answers = []

        async def answer(self, *args, **kwargs):
            self.answers.append((args, kwargs))

    query = OldButton()
    await bot.unbind_confirm(make_update(user_id=101, callback=query), ctx)
    assert "已失效" in query.answers[0][0][0]
    with transaction(factory) as session:
        assert accounts.is_registered(session, 101)
        assert session.get(CashAccount, replacement.id).balance == Decimal(500)


async def test_payment_only_grants_registered_user_and_expires(factory, accounts):
    class PaymentBot:
        def __init__(self):
            self.calls = []

        async def create_invoice_link(self, **kwargs):
            self.calls.append(kwargs)
            return "https://t.me/$test"

    payment_bot = PaymentBot()
    ctx = context(factory, accounts, bot_api=payment_bot)
    await bot.subscribe(make_update(Message(text="/subscribe")), ctx)
    assert not payment_bot.calls
    with transaction(factory) as session:
        accounts.register(session, 101, "+61400000001")
    offer = Message(text="/subscribe")
    await bot.subscribe(make_update(offer), ctx)
    assert payment_bot.calls[0]["currency"] == "XTR"
    assert payment_bot.calls[0]["prices"][0].amount == 400
    assert payment_bot.calls[0]["subscription_period"] == 2592000
    assert "联系管理员可以有优惠" in offer.replies[0][0]
    assert "https://t.me/ZhenAi_YongHeng" in offer.replies[0][0]
    assert offer.replies[0][1]["reply_markup"].inline_keyboard[1][0].url == "https://t.me/ZhenAi_YongHeng"

    checkout = SimpleNamespace(from_user=SimpleNamespace(id=101), invoice_payload="premium:101",
                               currency="XTR", total_amount=400, answers=[])

    async def answer(**kwargs):
        checkout.answers.append(kwargs)

    checkout.answer = answer
    await bot.pre_checkout(make_update(chat_type=None, checkout=checkout), ctx)
    assert checkout.answers[-1]["ok"] is True
    checkout.total_amount = 399
    await bot.pre_checkout(make_update(chat_type=None, checkout=checkout), ctx)
    assert checkout.answers[-1]["ok"] is False

    expires = datetime.now(UTC) + timedelta(days=30)
    payment = SimpleNamespace(currency="XTR", total_amount=400, invoice_payload="premium:101",
                              telegram_payment_charge_id="charge-1", subscription_expiration_date=expires)
    paid = Message(payment=payment)
    await bot.successful_payment(make_update(paid), ctx)
    assert "付费 Premium 已激活" in paid.replies[0][0]
    assert "/strategy1" in paid.replies[0][0]
    await bot.successful_payment(make_update(Message(payment=payment)), ctx)
    with transaction(factory) as session:
        assert accounts.has_premium_access(session, 101)
        assert not accounts.has_premium_access(session, 101, now=expires + timedelta(seconds=1))


async def test_unbind_cancels_active_star_subscription_before_deleting_data(factory, accounts):
    with transaction(factory) as session:
        accounts.register(session, 101, "+61400000001")
        accounts.record_subscription_payment(session, 101, "charge-1", 400,
                                             datetime.now(UTC) + timedelta(days=30))

    class PaymentBot:
        def __init__(self):
            self.calls = []
            self.succeeds = False

        async def edit_user_star_subscription(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return self.succeeds

    class Query:
        def __init__(self, data):
            self.data = data
            self.edits = []

        async def answer(self, *args, **kwargs):
            pass

        async def edit_message_text(self, text):
            self.edits.append(text)

    payment_bot = PaymentBot()
    ctx = context(factory, accounts)
    ctx.application.bot = payment_bot
    ctx.application.bot_data["check_waiters"] = {}
    ctx.user_data = {}
    start_message = Message(text="/unbind")
    await bot.unbind_start(make_update(start_message), ctx)
    query = Query(start_message.replies[0][1]["reply_markup"].inline_keyboard[0][0].callback_data)
    update = make_update(user_id=101, callback=query)
    await bot.unbind_confirm(update, ctx)
    with transaction(factory) as session:
        assert accounts.is_registered(session, 101)
    assert "未删除" in query.edits[-1]
    payment_bot.succeeds = True
    await bot.unbind_confirm(update, ctx)
    assert payment_bot.calls[-1][0] == (101, "charge-1")
    with transaction(factory) as session:
        assert not accounts.is_registered(session, 101)


def test_admin_still_needs_registration_and_phone_cannot_be_reused(factory, accounts):
    with transaction(factory) as session:
        assert not accounts.is_registered(session, 999)
        assert not accounts.has_premium_access(session, 999)
        accounts.register(session, 999, "+61400000999")
        assert accounts.has_premium_access(session, 999)
        accounts.register(session, 101, "+61400000001")
    with pytest.raises(ValueError, match="already bound"), transaction(factory) as session:
        accounts.register(session, 102, "+61 400 000 001")


def test_gate_is_first_and_monthly_jobs_skip_nonpremium(factory, accounts):
    from quantbot import strategy_service

    app = Application.builder().token("123456:TEST").build()
    bot.configure_application(app, accounts, factory)
    assert isinstance(app.handlers[-2][0], TypeHandler)
    assert app.handlers[-2][0].callback is bot.authorization_gate

    with transaction(factory) as session:
        accounts.register(session, 999, "+61400000999")
        premium = accounts.register(session, 101, "+61400000001").user
        unpaid = accounts.register(session, 102, "+61400000002").user
        accounts.whitelist_add(session, 999, "+61 400 000 001")
        session.add(UserStrategy(user_id=premium.id, strategy_id="strategy1"))
        session.flush()
        due = strategy_service.due_notice_users(session, datetime(2026, 10, 12, 2, tzinfo=UTC),
                                                1, "Asia/Shanghai", accounts)
        assert premium.id in due
        assert unpaid.id not in due


def test_premium_migration_keeps_legacy_portfolio_and_history(tmp_path, monkeypatch):
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from alembic import command

    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(cfg, "0003_user_chat")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id,telegram_id,phone_fingerprint,phone_encrypted,role,is_active,created_at,updated_at) "
                          "VALUES ('old',101,'fingerprint','old ciphertext','USER',1,'2026-01-01','2026-01-01')"))
        conn.execute(text("INSERT INTO cash_accounts (user_id,balance,currency,management_mode,updated_at) "
                          "VALUES ('old',123.45,'USD','MANUAL','2026-01-01')"))
        conn.execute(text("INSERT INTO positions (user_id,symbol,exchange,shares,average_cost,currency,created_at,updated_at) "
                          "VALUES ('old','AAPL','US',2,100,'USD','2026-01-01','2026-01-01')"))
        conn.execute(text("INSERT INTO transactions (user_id,type,actor_telegram_id,created_at) "
                          "VALUES ('old','POSITION_CREATE',101,'2026-01-01')"))
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT phone_fingerprint FROM users WHERE id='old'")).scalar() == "fingerprint"
        assert conn.execute(text("SELECT phone_encrypted FROM users WHERE id='old'")).scalar() == "old ciphertext"
        assert conn.execute(text("SELECT balance FROM cash_accounts WHERE user_id='old'")).scalar() == 123.45
        assert conn.execute(text("SELECT shares FROM positions WHERE user_id='old'")).scalar() == 2
        assert conn.execute(text("SELECT count(*) FROM transactions WHERE user_id='old'")).scalar() == 1
        assert conn.execute(text("SELECT strategy_id FROM user_strategies WHERE user_id='old'")).scalar() == "strategy1"
        assert conn.execute(text("SELECT weight_percent FROM user_strategies WHERE user_id='old'")).scalar() == 100
    command.downgrade(cfg, "0011_multi_strategy")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO user_strategies (user_id,strategy_id,enabled_at) "
                          "VALUES ('old','strategy2','2026-01-01')"))
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM user_strategies "
                                 "WHERE user_id='old' AND weight_percent IS NULL")).scalar() == 2
        assert conn.execute(text("SELECT balance FROM cash_accounts WHERE user_id='old'")).scalar() == 123.45
    engine.dispose()

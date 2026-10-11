from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from quantbot import bot, strategy_service as svc
from quantbot.models import Position, Transaction, User
from quantbot.services import PortfolioService
from quantbot.strategy_text import render_cooldowns
from tests.test_strategy_service import SETTINGS, add_user, make_run


class Query:
    def __init__(self, data):
        self.data, self.edits, self.answers = data, [], []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def args(factory, accounts, telegram_id, data, user_data=None):
    query = Query(data)
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=telegram_id))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"session_factory": factory, "accounts": accounts}),
                              user_data=user_data or {})
    return update, context, query


def latest(factory, uid):
    with factory.begin() as session:
        return session.scalar(select(Transaction).where(Transaction.user_id == uid).order_by(Transaction.id.desc()))


@pytest.mark.parametrize("command", ["edit", "remove"])
async def test_sale_defaults_to_block_and_can_be_declined_after_restart(factory, accounts, command):
    uid = add_user(factory, 1, positions=[("AAA", "5")])
    data = {"edit": ("AAA", "3", "100", "US")} if command == "edit" else {"remove": ("AAA", "US")}
    update, context, query = args(factory, accounts, 1, f"{command}:yes", data)
    await getattr(bot, f"{command}_confirm")(update, context)
    row = latest(factory, uid)
    text, options = query.edits[0]
    assert "默认设置：加入" in text and "没有实际卖出" in text
    assert all(secret not in text for secret in ("180", "365", "天", "到期"))
    assert options["reply_markup"].inline_keyboard[0][1].callback_data == f"sale-cooldown:{row.id}:no"
    with factory.begin() as session:
        blocked, protected = svc.cooldowns(session, uid)
        assert "AAA" in blocked and protected == {}
        assert "AAA" not in dict(svc.signals_for(session, uid, make_run(), SETTINGS, datetime.now(UTC))["summary"]["top_candidates"])
    # Empty user_data simulates a restart: the choice is tied to a persisted transaction.
    update, context, query = args(factory, accounts, 1, f"sale-cooldown:{row.id}:no")
    await bot.sale_cooldown_confirm(update, context)
    with factory.begin() as session:
        assert svc.cooldowns(session, uid) == ({}, {})
        holding = PortfolioService.get_position(session, uid, "AAA")
        assert (holding.shares == Decimal("3")) if command == "edit" else holding is None
    assert "持仓修改已保存" in query.edits[0][0]
    # Replayed callbacks do not change a completed choice.
    update, context, _ = args(factory, accounts, 1, f"sale-cooldown:{row.id}:yes")
    await bot.sale_cooldown_confirm(update, context)
    assert latest(factory, uid).after_data["sale_cooldown"] == {"enabled": False, "answered": True}


@pytest.mark.parametrize("shares,cost", [("5", "80"), ("7", "100")])
async def test_cost_changes_and_increases_do_not_prompt(factory, accounts, shares, cost):
    uid = add_user(factory, 1, positions=[("AAA", "5")])
    update, context, query = args(factory, accounts, 1, "edit:yes", {"edit": ("AAA", shares, cost, "US")})
    await bot.edit_confirm(update, context)
    assert "冷却" not in query.edits[0][0] and query.edits[0][1]["reply_markup"] is None
    with factory.begin() as session:
        assert svc.cooldowns(session, uid) == ({}, {})


async def test_choice_is_owned_by_user_and_never_restarts_clock(factory, accounts):
    uid = add_user(factory, 1, positions=[("AAA", "5")])
    add_user(factory, 2)
    with factory.begin() as session:
        PortfolioService().remove_position(session, session.get(User, uid), "AAA", actor=1, record_sale=True)
    row = latest(factory, uid)
    at = datetime.now(UTC) - timedelta(days=10)
    with factory.begin() as session:
        session.get(Transaction, row.id).created_at = at
    update, context, query = args(factory, accounts, 2, f"sale-cooldown:{row.id}:no")
    await bot.sale_cooldown_confirm(update, context)
    assert query.answers == ["这条记录不可用。"] and not query.edits
    for _ in range(2):
        update, context, _ = args(factory, accounts, 1, f"sale-cooldown:{row.id}:yes")
        await bot.sale_cooldown_confirm(update, context)
    from quant_strategy import logic
    with factory.begin() as session:
        blocked, _ = svc.cooldowns(session, uid)
        assert blocked["AAA"] == at + timedelta(days=logic.K1A.block_days)
        assert "AAA" not in render_cooldowns(blocked, {}, blocked["AAA"] + timedelta(seconds=1))
    shown = render_cooldowns(blocked, {}, datetime.now(UTC))
    for secret in ("180", "365", "天", blocked["AAA"].strftime("%Y-%m-%d")):
        assert secret not in shown


async def test_declining_new_sale_preserves_earlier_block_and_legacy_edits_are_ignored(factory, accounts):
    uid = add_user(factory, 1, positions=[("AAA", "10")])
    with factory.begin() as session:
        user = session.get(User, uid)
        service = PortfolioService()
        service.update_position(session, user, "AAA", shares="9", actor=1)
    with factory.begin() as session:
        assert svc.cooldowns(session, uid) == ({}, {})
        service.update_position(session, session.get(User, uid), "AAA", shares="8", actor=1, record_sale=True)
    with factory.begin() as session:
        original = svc.cooldowns(session, uid)[0]["AAA"]
        service.remove_position(session, session.get(User, uid), "AAA", actor=1, record_sale=True)
    row = latest(factory, uid)
    update, context, _ = args(factory, accounts, 1, f"sale-cooldown:{row.id}:no")
    await bot.sale_cooldown_confirm(update, context)
    with factory.begin() as session:
        assert svc.cooldowns(session, uid)[0]["AAA"] == original


async def test_cancelled_edit_and_remove_do_not_create_sales(factory, accounts):
    uid = add_user(factory, 1, positions=[("AAA", "5")])
    for command in ("edit", "remove"):
        update, context, _ = args(factory, accounts, 1, f"{command}:no")
        await getattr(bot, f"{command}_confirm")(update, context)
    with factory.begin() as session:
        assert session.scalar(select(Transaction).where(Transaction.user_id == uid)) is None
        assert session.scalar(select(Position).where(Position.user_id == uid)).shares == 5


def test_sale_cooldown_keeps_foreign_ticker_separate_and_failed_edits_roll_back(factory):
    uid = add_user(factory, 1, positions=[("AAA", "5")])
    with factory.begin() as session:
        user = session.get(User, uid)
        service = PortfolioService()
        service.add_position(session, user, "AAA", 3, 20, exchange="LSE", actor=1)
        service.remove_position(session, user, "AAA", "LSE", actor=1, record_sale=True)
    with pytest.raises(ValueError), factory.begin() as session:
        service.update_position(session, session.get(User, uid), "AAA", shares="2", average_cost="invalid", actor=1, record_sale=True)
    with factory.begin() as session:
        assert set(svc.cooldowns(session, uid)[0]) == {"AAA@LSE:USD"}
        assert PortfolioService.get_position(session, uid, "AAA").shares == 5

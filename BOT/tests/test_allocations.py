from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from quantbot import bot
from quantbot import strategy_service as svc
from quantbot.allocations import allocation_fraction, parse_weights, save_weights
from quantbot.models import CashAccount, UserStrategy
from tests.test_premium_access import Message, context, make_update
from tests.test_strategy_service import SETTINGS, add_user, make_run


@pytest.mark.parametrize("args", [[], ["1=0"], ["1=101"], ["1=-1"], ["1=nan"],
                                   ["1=1e2"], ["1=100.001"], ["1=50", "1=50"],
                                   ["0=100"], ["1=60", "2=39.99"], ["1", "100"]])
def test_invalid_inputs(args):
    with pytest.raises(ValueError):
        parse_weights(args)


def test_allocations_persist_scale_only_virtual_portfolio_and_isolate_users(factory):
    uid = add_user(factory, 1, cash="200", positions=[("AAA", "5")])
    other = add_user(factory, 2, cash="200")
    with factory.begin() as session:
        session.add(UserStrategy(user_id=uid, strategy_id="strategy2"))
    with factory.begin() as session:
        with pytest.raises(ValueError):
            allocation_fraction(session, uid)
        save_weights(session, uid, parse_weights(["1=60%", "2=40"]))
    with factory.begin() as session:
        assert allocation_fraction(session, uid) == Decimal("0.6")
        assert allocation_fraction(session, other) == 1
        portfolio = svc.portfolio_of(session, uid)
        assert portfolio.cash == 120
        assert portfolio.holdings["AAA"].shares == 3
        assert session.get(CashAccount, uid).balance == 200
        assert svc.signals_for(session, uid, make_run(), SETTINGS)["summary"]["equity"] == 420
        with pytest.raises(ValueError):
            save_weights(session, uid, parse_weights(["1=100"]))
        assert allocation_fraction(session, uid) == Decimal("0.6")


async def test_invalid_allocation_stops_generation_and_pending_delivery(factory):
    uid = add_user(factory, 1, cash="200")
    with factory.begin() as session:
        run, _ = svc.store_run(session, make_run())
        run_id = run.id
    svc.generate_for_all_users(factory, run_id, SETTINGS)
    with factory.begin() as session:
        session.add(UserStrategy(user_id=uid, strategy_id="strategy2"))
    app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()),
                          bot_data={"session_factory": factory})
    assert await svc.deliver_pending(app) == (0, 0)
    assert svc.generate_for_all_users(factory, run_id, SETTINGS)["with_signals"] == 0
    with factory.begin() as session:
        save_weights(session, uid, parse_weights(["1=60", "2=40"]))
    assert await svc.deliver_pending(app) == (0, 0)  # Prior allocation's suggestion remains invalid.
    app.bot.send_message.assert_not_awaited()


async def test_weight_command_supports_separate_message_and_cancel(factory, accounts):
    uid = add_user(factory, 999)
    ctx = context(factory, accounts)
    ctx.user_data = {}
    await bot._staged_command_entry(make_update(Message(text="/strategy_weights"), user_id=999), ctx)
    assert ctx.user_data["pending_command_args"][0] == "strategy_weights"
    await bot._staged_command_data(make_update(Message(text="1=100"), user_id=999), ctx)
    assert not ctx.user_data
    with factory.begin() as session:
        assert allocation_fraction(session, uid) == 1
    ctx.args = []
    await bot._staged_command_entry(make_update(Message(text="/strategy_weights"), user_id=999), ctx)
    await bot.cancel(make_update(Message(text="/cancel"), user_id=999), ctx)
    assert not ctx.user_data

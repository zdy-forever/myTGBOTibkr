from decimal import Decimal

import pytest

from quantbot import bot
from quantbot.help_text import (
    STRATEGY_HELP_BLOCKS,
    STRATEGY_WELCOME_BLOCKS,
    help_pages,
    strategy_management_text,
)
from quantbot.models import UserStrategy
from tests.test_premium_access import Message, context, make_update


@pytest.mark.parametrize("enabled", [set(), {"strategy1"}, {"strategy2"}, {"strategy1", "strategy2"}])
async def test_help_includes_exactly_selected_strategy_blocks(factory, accounts, enabled):
    with factory.begin() as session:
        user = accounts.register(session, 999, "+61400000999").user
        for strategy_id in enabled:
            session.add(UserStrategy(user_id=user.id, strategy_id=strategy_id))
    ctx = context(factory, accounts)
    message = Message(text="/help")
    await bot.help_command(make_update(message, user_id=999), ctx)
    pages = [text for text, _ in message.replies]
    for strategy_id, block in STRATEGY_HELP_BLOCKS.items():
        assert (block in pages) == (strategy_id in enabled)
    assert "/admin_help" in pages[0]
    assert all(len(page) < 4096 for page in pages)
    welcome = bot._welcome_text("admin", enabled)
    for strategy_id, block in STRATEGY_WELCOME_BLOCKS.items():
        assert (block in welcome) == (strategy_id in enabled)
    # Unknown topics must use the same selected-strategy overview.
    ctx.args = ["unknown"]
    fallback = Message(text="/help unknown")
    await bot.help_command(make_update(fallback, user_id=999), ctx)
    assert [text for text, _ in fallback.replies] == pages


async def test_strategy_two_only_does_not_get_strategy_one_topic_or_commands(factory, accounts):
    with factory.begin() as session:
        user = accounts.register(session, 999, "+61400000999").user
        session.add(UserStrategy(user_id=user.id, strategy_id="strategy2"))
    ctx = context(factory, accounts)
    message = Message(text="/help_import")
    await bot.help_import(make_update(message, user_id=999), ctx)
    assert "CASH,0" in message.replies[0][0]
    message = Message(text="/help_faq")
    await bot.help_faq(make_update(message, user_id=999), ctx)
    assert "/auto_notice_date" not in message.replies[0][0]
    ctx.args = []
    message = Message(text="/strategy")
    await bot.strategy_command(make_update(message, user_id=999), ctx)
    assert "策略 2：G2 趋势 ETF 轮动 · 已启用" in message.replies[0][0]
    assert "/strategy1_off" not in message.replies[0][0]
    assert "/check1" not in message.replies[0][0]
    assert "/check 检查所有已启用策略" in message.replies[0][0]


def test_disabling_strategy_removes_its_help_and_unknown_ids_do_not_leak():
    both = help_pages({"strategy1", "strategy2"})
    only_two = help_pages({"strategy2", "unknown"})
    assert both[1:] == [STRATEGY_HELP_BLOCKS["strategy1"], STRATEGY_HELP_BLOCKS["strategy2"]]
    assert only_two[1:] == [STRATEGY_HELP_BLOCKS["strategy2"]]
    assert "/strategy1_off" not in "\n".join(help_pages(set()))


def test_strategy_management_available_actions_and_weight_status():
    empty = strategy_management_text({}, False)
    assert "策略管理" in empty and "/strategy1_off" not in empty
    assert "CASH 0" in empty and "/strategy2" in empty
    enabled = strategy_management_text({"strategy1": Decimal(100)}, True)
    assert "/strategy1_off" in enabled and "/strategy_weights" in enabled
    assert "合计 100%" in enabled
    pending = strategy_management_text({"strategy1": Decimal(60), "strategy2": None}, False)
    assert "才能生成建议" in pending and "待分配" in pending
    assert "/strategy2_off" in pending and "研发中" not in pending
    assert all(len(page) < 4096 for page in (empty, enabled, pending))

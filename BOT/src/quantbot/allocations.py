"""User-specific allocation validation shared by commands and background jobs."""
import re
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from .models import CashAccount, StrategyCash, UserStrategy


def cash_budgets(session, user_id):
    return list(session.scalars(select(StrategyCash).where(
        StrategyCash.user_id == user_id).order_by(StrategyCash.strategy_id)))


def invalidate_suggestions(session, user_id):
    for row in selections(session, user_id):
        row.enabled_at = datetime.now(UTC)


def set_strategy_cash(session, user, number, amount, currency, *, actor):
    """Explicit CASH 0 replaces the entire cash allocation; CASH N sets one budget."""
    from .services import PortfolioService, decimal_value
    if not isinstance(number, int) or not 0 <= number <= 9999:
        raise ValueError("策略编号须为 0–9999；0 表示公共现金池。")
    amount = decimal_value(amount, max_places=8)
    if number and currency != "USD":
        raise ValueError("独立策略现金只接受 USD。")
    budgets = cash_budgets(session, user.id)
    if number == 0:
        for row in budgets:
            session.delete(row)
        PortfolioService().set_cash(session, user, amount, currency, actor=actor)
    else:
        key = "strategy" + str(number)
        row = session.get(StrategyCash, (user.id, key))
        if row is None:
            row = StrategyCash(user_id=user.id, strategy_id=key, balance=amount)
            session.add(row)
        else:
            row.balance = amount
        session.flush()
        total = sum((r.balance for r in cash_budgets(session, user.id)), Decimal(0))
        PortfolioService().set_cash(session, user, total, "USD", actor=actor)
    invalidate_suggestions(session, user.id)
    session.flush()


def allocated_cash(session, user_id, strategy_id="strategy1"):
    fraction = allocation_fraction(session, user_id, strategy_id)
    budget = session.get(StrategyCash, (user_id, strategy_id))
    if budget is not None:
        return budget.balance, "USD"
    row = session.get(CashAccount, user_id)
    return (row.balance * fraction, row.currency) if row else (Decimal(0), "USD")


def selections(session, user_id):
    return session.scalars(select(UserStrategy).where(UserStrategy.user_id == user_id)
                           .order_by(UserStrategy.strategy_id)).all()


def allocation_fraction(session, user_id, strategy_id="strategy1") -> Decimal:
    rows = selections(session, user_id)
    if strategy_id not in {row.strategy_id for row in rows}:
        raise ValueError("请先用 /strategy 启用对应策略。")
    budgets = cash_budgets(session, user_id)
    if budgets:
        chosen = next((row for row in budgets if row.strategy_id == strategy_id), None)
        total = sum((row.balance for row in budgets), Decimal(0))
        if chosen is None or total <= 0:
            raise ValueError("当前为独立金额模式，请用 CASH 策略编号 金额USD 设置该策略现金。")
        # The same fraction partitions existing holdings, so no asset is counted twice.
        return chosen.balance / total
    if (not rows or any(row.weight_percent is None or not 0 < row.weight_percent <= 100 for row in rows)
            or sum(row.weight_percent for row in rows) != 100):
        raise ValueError("请先用 /strategy_weights 为所有已启用策略分配权重，总和必须为 100%。分配完成前暂停策略建议和推送。")
    chosen = next((row for row in rows if row.strategy_id == strategy_id), None)
    if chosen is None:
        raise ValueError("请先用 /strategy 启用对应策略。")
    return chosen.weight_percent / Decimal(100)


def parse_weights(args):
    if not args or len(args) > 20:
        raise ValueError("请输入策略编号=百分比，例如 1=100；多策略示例：1=60 2=40。")
    values = {}
    for item in args:
        match = re.fullmatch(r"([1-9][0-9]{0,3})=([0-9]{1,3}(?:\.[0-9]{1,2})?)%?", item)
        if not match:
            raise ValueError("格式应为 编号=百分比，最多两位小数；例如 1=100。")
        key, value = "strategy" + match[1], Decimal(match[2])
        if key in values or not 0 < value <= 100:
            raise ValueError("策略编号不能重复；每项权重必须大于 0 且不超过 100。停用请使用对应的停用命令。")
        values[key] = value
    if sum(values.values()) != 100:
        raise ValueError("所有策略权重之和必须等于 100%，设置未修改。")
    return values


def save_weights(session, user_id, values):
    if cash_budgets(session, user_id):
        raise ValueError("当前为独立金额模式，不能同时按比例分配。请先发送 CASH 0 总现金USD，替换全部独立现金，再设置比例。")
    if not values or any(not isinstance(value, Decimal) or not value.is_finite()
                         or not 0 < value <= 100 or value != value.quantize(Decimal("0.01"))
                         for value in values.values()) or sum(values.values()) != 100:
        raise ValueError("每项权重须大于 0，最多两位小数，且总和必须为 100%。")
    rows = selections(session, user_id)
    if set(values) != {row.strategy_id for row in rows}:
        raise ValueError("请完整填写所有已启用策略，不能填写尚未启用或研发中的策略。用 /strategy 查看名单。")
    if any(row.weight_percent != values[row.strategy_id] for row in rows):
        now = datetime.now(UTC)
        for row in rows:
            row.weight_percent = values[row.strategy_id]
            row.enabled_at = now  # Old pending suggestions use the old allocation.

"""User-specific allocation validation shared by commands and background jobs."""
import re
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from .models import UserStrategy


def selections(session, user_id):
    return session.scalars(select(UserStrategy).where(UserStrategy.user_id == user_id)
                           .order_by(UserStrategy.strategy_id)).all()


def allocation_fraction(session, user_id, strategy_id="strategy1") -> Decimal:
    rows = selections(session, user_id)
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

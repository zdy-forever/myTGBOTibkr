"""Admin commands for daily strategy email reminders."""
from __future__ import annotations

import asyncio
import html
import smtplib
from datetime import date

from sqlalchemy import select
from telegram.constants import ParseMode

from .db import transaction
from .email_service import (
    STRATEGY_IDS, compose, normalize_email, parse_strategies, send, strategy_numbers,
)
from .models import EmailDelivery, EmailSubscription, User

ADMIN_MENU = [("email_list", "查看邮件提醒"), ("email_add", "添加邮件提醒"),
              ("email_edit", "修改邮件提醒的策略"), ("email_remove", "删除邮件提醒"),
              ("email_test", "发送测试邮件")]
COMMANDS = {"/" + command for command, _ in ADMIN_MENU}
USAGE = ("用法：\n/email_add 邮箱 用户ID 策略　例如 /email_add a@qq.com 123456789 1,2\n"
         "/email_edit 邮箱 策略　例如 /email_edit a@qq.com 2\n/email_remove 邮箱\n/email_test 邮箱\n/email_list")


def _require_admin(context, session, telegram_id):
    context.application.bot_data["accounts"].require_maintainer(session, telegram_id)


async def _guard(update, context, work):
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            _require_admin(context, session, update.effective_user.id)
            text = work(session)
    except PermissionError:
        text = "仅已验证的管理员可管理邮件提醒。"
    except ValueError as exc:
        text = f"{exc}\n\n{USAGE}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def _numbers_text(value: str) -> str:
    return "、".join(f"策略{n}" for n in strategy_numbers(value)) or "无"


async def email_list(update, context):
    def work(session):
        rows = list(session.scalars(select(EmailSubscription).order_by(EmailSubscription.email)))
        if not rows:
            return "还没有邮件提醒。\n\n" + html.escape(USAGE)
        lines = ["📧 <b>邮件提醒</b>"]
        for row in rows:
            user = session.get(User, row.user_id)
            last = session.scalar(select(EmailDelivery).where(EmailDelivery.email == row.email)
                                  .order_by(EmailDelivery.session_date.desc(), EmailDelivery.id.desc()).limit(1))
            status = ("" if last is None else f"；最近 {last.session_date} " +
                      ("已发送" if last.sent_at else f"失败（{html.escape(last.last_error or '未知')}）"))
            who = f"用户ID <code>{user.telegram_id}</code>" if user else "用户已不存在"
            lines.append(f"• {html.escape(row.email)} → {who}，{_numbers_text(row.strategies)}{status}")
        if context.application.bot_data.get("smtp") is None:
            lines.append("\n⚠️ 服务器未配置 EMAIL_SENDER / EMAIL_PASSWORD，目前不会发送邮件。")
        return "\n".join(lines)
    await _guard(update, context, work)


async def email_add(update, context):
    def work(session):
        if len(context.args) != 3:
            raise ValueError("需要 3 个参数：邮箱、用户ID、策略。")
        email = normalize_email(context.args[0])
        if not context.args[1].isascii() or not context.args[1].isdecimal():
            raise ValueError("用户ID 必须是数字 Telegram ID。")
        strategies = parse_strategies(context.args[2])
        accounts = context.application.bot_data["accounts"]
        user = accounts.by_telegram(session, int(context.args[1]))
        if user is None:
            raise ValueError("找不到这个 Telegram ID 对应的已验证用户；对方需要先用 /start 验证手机号。")
        if session.get(EmailSubscription, email):
            raise ValueError("这个邮箱已经存在；改策略用 /email_edit，换绑定用户请先 /email_remove。")
        session.add(EmailSubscription(email=email, user_id=user.id, strategies=strategies,
                                      created_by=update.effective_user.id))
        return (f"已添加：{html.escape(email)} → 用户ID <code>{user.telegram_id}</code>，"
                f"{_numbers_text(strategies)}。\n该用户需要启用对应策略并设置资金，才会生成每日报告。")
    await _guard(update, context, work)


async def email_edit(update, context):
    def work(session):
        if len(context.args) != 2:
            raise ValueError("需要 2 个参数：邮箱、策略。")
        row = session.get(EmailSubscription, normalize_email(context.args[0]))
        if row is None:
            raise ValueError("没有这个邮箱。")
        row.strategies = parse_strategies(context.args[1])
        return f"已修改：{html.escape(row.email)} 现在订阅 {_numbers_text(row.strategies)}。"
    await _guard(update, context, work)


async def email_remove(update, context):
    def work(session):
        if len(context.args) != 1:
            raise ValueError("需要 1 个参数：邮箱。")
        row = session.get(EmailSubscription, normalize_email(context.args[0]))
        if row is None:
            raise ValueError("没有这个邮箱。")
        session.delete(row)
        return f"已删除 {html.escape(row.email)} 的邮件提醒。"
    await _guard(update, context, work)


async def email_test(update, context):
    config = context.application.bot_data.get("smtp")
    try:
        if len(context.args) != 1:
            raise ValueError("需要 1 个参数：邮箱。")
        email = normalize_email(context.args[0])
        with transaction(context.application.bot_data["session_factory"]) as session:
            _require_admin(context, session, update.effective_user.id)
    except PermissionError:
        await update.message.reply_text("仅已验证的管理员可管理邮件提醒。")
        return
    except ValueError as exc:
        await update.message.reply_text(f"{exc}\n\n{USAGE}")
        return
    if config is None:
        await update.message.reply_text("服务器未配置 EMAIL_SENDER / EMAIL_PASSWORD，无法发送。")
        return
    subject, body = compose(next(iter(STRATEGY_IDS)), date.today(), None, False)
    try:
        await asyncio.to_thread(send, config, email, "[测试] " + subject, body)
    except (smtplib.SMTPException, OSError) as exc:
        await update.message.reply_text(f"发送失败：{type(exc).__name__}。请检查授权码、SMTP服务是否开启。")
        return
    await update.message.reply_text(f"测试邮件已发送到 {email}（使用“无需操作”模板）。")


HANDLERS = {"email_list": email_list, "email_add": email_add, "email_edit": email_edit,
            "email_remove": email_remove, "email_test": email_test}

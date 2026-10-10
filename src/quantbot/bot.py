from __future__ import annotations

import io
import logging
from datetime import datetime

from sqlalchemy import desc, select
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .announcements import publish_announcement
from .csv_io import export_portfolio_csv, import_portfolio_csv
from .db import transaction
from .models import Announcement, NotificationPreference, PlanFrequency, Transaction
from .services import AccountService, PlanService, PortfolioService, system_stats

log = logging.getLogger(__name__)
CONTACT, ADD_DATA, EDIT_DATA, REMOVE_CONFIRM, CASH_DATA, PLAN_DATA, ANNOUNCE_TEXT, ANNOUNCE_CONFIRM = range(8)

HELP = """可用命令：
/portfolio 持仓与现金
/add 添加持仓
/edit 修改持仓
/remove 删除持仓
/cash 修改现金
/plan 设置定投
/import 导入 CSV（随后发送文件）
/export 导出 CSV
/signals 策略信号（等待策略接入）
/history 最近账户操作
/settings 通知设置
/cancel 取消当前操作"""


def _services(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["accounts"], PortfolioService()


async def _error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("telegram update failed", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("操作失败，数据未保存。请检查输入后重试。")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        if accounts.by_telegram(session, update.effective_user.id):
            await update.message.reply_text("欢迎回来。\n\n" + HELP)
            return ConversationHandler.END
    keyboard = [[KeyboardButton("分享我的手机号", request_contact=True)]]
    await update.message.reply_text(
        "首次使用需要绑定 Telegram 本人的手机号。手机号默认仅保存不可逆的密钥 HMAC 指纹。",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
    )
    return CONTACT


async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    item = update.message.contact
    if not item or item.user_id != update.effective_user.id:
        await update.message.reply_text("只能分享当前 Telegram 账号自己的联系人。")
        return CONTACT
    accounts, _ = _services(context)
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            accounts.register(session, update.effective_user.id, item.phone_number)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    await update.message.reply_text(
        "注册完成，账户已初始化。\n\n" + HELP,
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


def _require(session, update, context):
    return context.application.bot_data["accounts"].require(session, update.effective_user.id)


async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, portfolios = _services(context)
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            cash = portfolios.cash(session, user.id)
            positions = portfolios.list_positions(session, user.id)
            lines = [f"现金：{cash.balance} {cash.currency}（{cash.management_mode.value}）"]
            lines += [f"{p.symbol}@{p.exchange}: {p.shares} 股，均价 {p.average_cost} {p.currency}" for p in positions]
    except PermissionError:
        await update.message.reply_text("请先使用 /start 注册。")
        return
    await update.message.reply_text("\n".join(lines))


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("请输入：股票代码 股数 平均成本 币种 [交易所]\n例如：ISRG 0.55 374.21 USD NASDAQ")
    return ADD_DATA


async def add_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = update.message.text.split()
    if len(parts) not in (4, 5):
        await update.message.reply_text("格式不正确，请重新输入。")
        return ADD_DATA
    symbol, shares, cost, currency = parts[:4]
    exchange = parts[4] if len(parts) == 5 else "US"
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            PortfolioService().add_position(session, user, symbol, shares, cost, currency, exchange, actor=update.effective_user.id)
    except (ValueError, PermissionError) as exc:
        await update.message.reply_text(str(exc))
        return ConversationHandler.END
    await update.message.reply_text(f"已添加 {symbol.upper()}。")
    return ConversationHandler.END


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("请输入：股票代码 新股数 新平均成本 [交易所]\n例如：ISRG 0.75 380 US")
    return EDIT_DATA


async def edit_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = update.message.text.split()
    if len(parts) not in (3, 4):
        await update.message.reply_text("格式不正确，请重新输入。")
        return EDIT_DATA
    symbol, shares, cost = parts[:3]
    exchange = parts[3] if len(parts) == 4 else "US"
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            old = PortfolioService.get_position(session, user.id, symbol, exchange)
            if not old:
                raise LookupError("position not found")
            context.user_data["edit"] = (symbol, shares, cost, exchange)
            text = f"当前：{old.shares} 股，均价 {old.average_cost}\n新值：{shares} 股，均价 {cost}\n确认修改？"
    except (ValueError, LookupError, PermissionError) as exc:
        await update.message.reply_text(str(exc))
        return ConversationHandler.END
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("确认", callback_data="edit:yes"), InlineKeyboardButton("取消", callback_data="edit:no")]]))
    return REMOVE_CONFIRM


async def edit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "edit:no":
        await query.edit_message_text("已取消。")
        return ConversationHandler.END
    symbol, shares, cost, exchange = context.user_data.pop("edit")
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context)
        PortfolioService().update_position(session, user, symbol, shares=shares, average_cost=cost,
                                           exchange=exchange, actor=update.effective_user.id)
    await query.edit_message_text(f"已更新 {symbol.upper()}。")
    return ConversationHandler.END


async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.args:
        await update.message.reply_text("用法：/remove 股票代码 [交易所]")
        return ConversationHandler.END
    symbol, exchange = context.args[0], context.args[1] if len(context.args) > 1 else "US"
    context.user_data["remove"] = (symbol, exchange)
    await update.message.reply_text(f"确认删除 {symbol.upper()}@{exchange.upper()}？", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("确认删除", callback_data="remove:yes"), InlineKeyboardButton("取消", callback_data="remove:no")]]))
    return REMOVE_CONFIRM


async def remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "remove:no":
        await query.edit_message_text("已取消。")
        return ConversationHandler.END
    symbol, exchange = context.user_data.pop("remove")
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context)
        PortfolioService().remove_position(session, user, symbol, exchange, actor=update.effective_user.id)
    await query.edit_message_text(f"已删除 {symbol.upper()}。")
    return ConversationHandler.END


async def cash_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("请输入现金余额和币种，例如：1000 USD")
    return CASH_DATA


async def cash_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = update.message.text.split()
    if len(parts) != 2:
        return CASH_DATA
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context)
        PortfolioService().set_cash(session, user, parts[0], parts[1], actor=update.effective_user.id)
    await update.message.reply_text("现金余额已更新。")
    return ConversationHandler.END


async def plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("请输入：金额 币种 周期 下次执行时间(ISO 8601，含时区)\n周期：weekly/monthly/quarterly/yearly")
    return PLAN_DATA


async def plan_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = update.message.text.split()
    if len(parts) != 4:
        return PLAN_DATA
    try:
        frequency = PlanFrequency(parts[2].lower())
        when = datetime.fromisoformat(parts[3])
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            PlanService().set_plan(session, user, parts[0], parts[1], frequency, when)
    except (ValueError, PermissionError) as exc:
        await update.message.reply_text(str(exc))
        return ConversationHandler.END
    await update.message.reply_text("定投计划已保存。它只增加模拟账户现金，不会从银行划款。")
    return ConversationHandler.END


async def import_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["await_csv"] = True
    await update.message.reply_text("请发送 UTF-8 CSV 文件。现有同名持仓不会被覆盖。")


async def csv_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.pop("await_csv", False):
        return
    document = update.message.document
    if document.file_size and document.file_size > 1_000_000:
        await update.message.reply_text("CSV 文件不能超过 1 MB。")
        return
    raw = await (await document.get_file()).download_as_bytearray()
    try:
        content = bytes(raw).decode("utf-8-sig")
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            result = import_portfolio_csv(session, user, content, actor=update.effective_user.id)
    except (UnicodeDecodeError, ValueError) as exc:
        await update.message.reply_text(f"导入失败，未保存任何数据：{exc}")
        return
    await update.message.reply_text(f"导入完成：{result.positions_created} 条持仓。")


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context)
        content = export_portfolio_csv(session, user)
    await update.message.reply_document(document=io.BytesIO(content.encode()), filename="portfolio.csv")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context)
        rows = session.scalars(select(Transaction).where(Transaction.user_id == user.id).order_by(desc(Transaction.created_at)).limit(20)).all()
        text = "\n".join(f"{row.created_at:%Y-%m-%d %H:%M} {row.type.value} {row.symbol or ''}" for row in rows) or "暂无记录。"
    await update.message.reply_text(text)


async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("量化策略尚未接入，因此当前不会生成或推送任何信号。")


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context)
        prefs = session.get(NotificationPreference, user.id)
        if context.args and context.args[0] in {"signals", "announcements"}:
            field = context.args[0] + "_enabled"
            setattr(prefs, field, not getattr(prefs, field))
        text = f"信号通知：{'开' if prefs.signals_enabled else '关'}\n公告通知：{'开' if prefs.announcements_enabled else '关'}\n切换：/settings signals 或 /settings announcements"
    await update.message.reply_text(text)


async def announce_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    with transaction(context.application.bot_data["session_factory"]) as session:
        context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
    await update.message.reply_text("请输入公告正文。")
    return ANNOUNCE_TEXT


async def announce_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if len(update.message.text) > 3500:
        await update.message.reply_text("公告不能超过 3500 字。")
        return ANNOUNCE_TEXT
    context.user_data["announcement"] = update.message.text
    await update.message.reply_text("预览：\n\n" + update.message.text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("确认发布", callback_data="announce:yes"), InlineKeyboardButton("取消", callback_data="announce:no")]]))
    return ANNOUNCE_CONFIRM


async def announce_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "announce:no":
        await query.edit_message_text("已取消。")
        return ConversationHandler.END
    with transaction(context.application.bot_data["session_factory"]) as session:
        context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
        item = Announcement(content=context.user_data.pop("announcement"), created_by_telegram_id=update.effective_user.id)
        session.add(item)
        session.flush()
        announcement_id = item.id
    await query.edit_message_text("公告已保存，开始后台推送。")
    context.application.create_task(publish_announcement(context.application, announcement_id), update=update)
    return ConversationHandler.END


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
        stats = system_stats(session)
    await update.message.reply_text(f"用户数：{stats['users']}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
        stats = system_stats(session)
    await update.message.reply_text("\n".join(f"{k}: {v}" for k, v in stats.items()))


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
        session.execute(select(1))
    await update.message.reply_text("OK：Telegram 与数据库可用；量化策略未接入。")


async def run_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
    await update.message.reply_text("量化策略未接入，未执行任何分析。")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("已取消。", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def process_plans(context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        count = PlanService().process_due(session)
    if count:
        log.info("processed investment plans", extra={"count": count})


def configure_application(application: Application, accounts: AccountService, factory) -> Application:
    application.bot_data.update(accounts=accounts, session_factory=factory)
    cancel_handler = CommandHandler("cancel", cancel)
    application.add_handler(ConversationHandler([CommandHandler("start", start)], {CONTACT: [MessageHandler(filters.CONTACT, contact)]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("add", add_start)], {ADD_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_data)]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("edit", edit_start)], {EDIT_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_data)], REMOVE_CONFIRM: [CallbackQueryHandler(edit_confirm, pattern=r"^edit:")]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("remove", remove_start)], {REMOVE_CONFIRM: [CallbackQueryHandler(remove_confirm, pattern=r"^remove:")]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("cash", cash_start)], {CASH_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, cash_data)]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("plan", plan_start)], {PLAN_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_data)]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("announce", announce_start)], {ANNOUNCE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, announce_text)], ANNOUNCE_CONFIRM: [CallbackQueryHandler(announce_confirm, pattern=r"^announce:")]}, fallbacks=[cancel_handler]))
    for command, handler in [("help", help_command), ("portfolio", portfolio), ("import", import_start), ("export", export_command), ("history", history), ("signals", signals), ("settings", settings), ("users", users_command), ("stats", stats_command), ("health", health), ("runstrategy", run_strategy)]:
        application.add_handler(CommandHandler(command, handler))
    application.add_handler(MessageHandler(filters.Document.FileExtension("csv"), csv_document))
    application.add_error_handler(_error)
    if application.job_queue:
        application.job_queue.run_repeating(process_plans, interval=3600, first=10, name="investment-plans")
    return application

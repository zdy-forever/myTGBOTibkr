from __future__ import annotations

import html
import io
import logging
from datetime import UTC, datetime

from sqlalchemy import desc, select
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
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
from .display import price, qty, render_portfolio
from .help_text import QUICK, help_for
from .models import Announcement, NotificationPreference, Transaction
from .services import AccountService, PortfolioService, parse_money_text, system_stats
from .strategy_text import render_cooldowns

log = logging.getLogger(__name__)
HISTORY_TEXT = {"position_create": "新增持仓", "position_update": "修改持仓", "position_delete": "删除持仓", "cash_set": "设置现金", "cash_adjust": "调整现金",
                "plan_contribution": "定投入账", "csv_import": "导入 CSV"}
CONTACT, ADD_DATA, EDIT_DATA, REMOVE_CONFIRM, CASH_DATA, ANNOUNCE_TEXT, ANNOUNCE_CONFIRM = range(7)

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
            await update.message.reply_text("👋 欢迎回来。\n\n" + QUICK, parse_mode=ParseMode.HTML)
            return ConversationHandler.END
    await update.message.reply_text(BIND_TEXT, reply_markup=_contact_keyboard())
    return CONTACT


BIND_TEXT = (
    "首次使用需要绑定你本人的手机号（只用来识别你本人，默认只保存不可逆的指纹，不会保存号码本身）。\n\n"
    "👇 怎么绑定：\n"
    "1. 点输入框下方的按钮「📱 分享我的手机号」。\n"
    "2. 在弹出的确认框里选「分享」。\n\n"
    "没看到按钮？点输入框右边的小键盘图标（或四个点的图标）展开。电脑版/网页版 Telegram 可能不显示这个按钮，请用手机版。\n"
    "注意：不能手动输入号码，也不能转发别人的名片，只有按钮分享的才有效。\n"
    "想先了解这个机器人：/help"
)


def _contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("📱 分享我的手机号", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)


async def contact_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Anything other than the shared contact while waiting for the phone number: say how to do it instead of staying silent."""
    await update.message.reply_text("需要用按钮分享手机号，不能手动输入。\n\n" + BIND_TEXT, reply_markup=_contact_keyboard())
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
        text = "这个手机号已经绑定了另一个 Telegram 账号。为防止被冒用，不能自动换绑，请联系管理员核实后处理。" if "already bound" in str(exc) else str(exc)
        await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    await update.message.reply_text(
        "✅ <b>注册完成</b>，账户已初始化。\n\n" + QUICK,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def help_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.args = ["导入"]
    await help_command(update, context)


async def help_faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.args = ["问题"]
    await help_command(update, context)


async def unbind_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        try:
            _require(session, update, context)
        except PermissionError:
            await update.message.reply_text("你还没有绑定，不需要解绑。发 /start 开始绑定。")
            return
    await update.message.reply_text(
        "⚠️ 解绑会删除你在这个机器人里的全部数据：手机号绑定、持仓、现金、操作记录和建议。不能撤销。\n"
        "建议先用 /export 导出你的持仓。解绑后可以随时用 /start 重新绑定（同一个或另一个手机号）。\n\n确定要解绑吗？",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("确认解绑并删除所有数据", callback_data="unbind:yes"), InlineKeyboardButton("取消", callback_data="unbind:no")]]),
    )


async def unbind_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes only the account of whoever pressed the button (never anyone else's)."""
    query = update.callback_query
    await query.answer()
    if query.data == "unbind:no":
        await query.edit_message_text("已取消，什么都没有改变。")
        return
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        done = accounts.unregister(session, update.effective_user.id)
    context.user_data.clear()
    waiters = context.application.bot_data.get("check_waiters", {})
    for key in [k for k, telegram_id in waiters.items() if telegram_id == update.effective_user.id]:
        del waiters[key]
    if done:
        log.info("a user unbound and their data was deleted")
    await query.edit_message_text("已解绑，你的全部数据已删除。需要时发 /start 重新绑定。" if done else "你还没有绑定。发 /start 开始绑定。")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        registered = accounts.by_telegram(session, update.effective_user.id) is not None
    await update.message.reply_text(help_for(context.args[0] if context.args else None, registered), parse_mode=ParseMode.HTML)


def _require(session, update, context):
    return context.application.bot_data["accounts"].require(session, update.effective_user.id)


async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, portfolios = _services(context)
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            cash = portfolios.cash(session, user.id)
            positions = portfolios.list_positions(session, user.id)
            text = render_portfolio(cash, positions)
    except PermissionError:
        await update.message.reply_text("请先使用 /start 注册。")
        return
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


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
            text = f"当前：{qty(old.shares)} 股，均价 {price(old.average_cost)}\n新值：{qty(shares)} 股，均价 {price(cost)}\n确认修改？"
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
    await update.message.reply_text("请输入现金余额，例如：1000 USD（也可以写 1000usd、$1000、1,000.5 美元；不写币种按美元）。策略只分析美元金额。")
    return CASH_DATA


async def cash_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount, currency = parse_money_text(update.message.text)
    except ValueError:
        await update.message.reply_text("没看懂这个金额。请这样输入：1000 USD（也可以写 1000usd、$1000、1,000.5 美元）。取消请发 /cancel。")
        return CASH_DATA
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context)
        PortfolioService().set_cash(session, user, amount, currency, actor=update.effective_user.id)
    note = "" if currency == "USD" else "\n注意：策略只分析美元金额，其他币种的现金不会参与建议。"
    await update.message.reply_text(f"现金已更新为 {amount:,.2f} {currency}。{note}")
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
        body = "\n".join(f"{row.created_at:%m-%d %H:%M}  {HISTORY_TEXT.get(row.type.value, row.type.value)} {row.symbol or ''}".rstrip() for row in rows)
    await update.message.reply_text(("🕘 <b>最近的账户操作</b>\n<pre>" + html.escape(body) + "</pre>") if body else "暂无记录。", parse_mode=ParseMode.HTML)


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Today's scores and suggestions for the CALLER only (own portfolio, own D scores). If the shared data is older than 12 hours it is shown with a notice while a refresh runs in the
    background; the caller then gets the fresh result automatically. Suggestions only: nothing is ordered."""
    from . import strategy_service as strategy

    app = context.application
    try:
        with transaction(app.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            settings = strategy.strategy_settings()
            row = strategy.latest_completed(session)
            fresh = row is not None and strategy.is_fresh(row)
            text = strategy.check_text(session, user.id, row, settings) if row is not None else ""
            recent = None if fresh else strategy.recent_attempt(session)
            user_id = user.id
    except PermissionError:
        await update.message.reply_text("请先使用 /start 注册。")
        return
    except strategy.StrategyUnavailable as exc:
        await update.message.reply_text(str(exc))
        return
    if fresh:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return
    notice = ("\n\n⏳ 以上数据不是最新的，正在后台更新（首次可能要几分钟），完成后会自动把最新结果发给你。" if text else "⏳ 还没有可用的数据，正在生成（首次可能要几分钟），完成后会自动发给你。")
    if recent is not None and not app.bot_data.get("check_job_running"):
        await update.message.reply_text((text + "\n\n" if text else "") + "刚刚有一次更新尝试，请 30 分钟后再用 /check。", parse_mode=ParseMode.HTML)
        return
    app.bot_data.setdefault("check_waiters", {})[user_id] = update.effective_user.id
    await update.message.reply_text(text + notice, parse_mode=ParseMode.HTML)
    if not app.bot_data.get("check_job_running"):
        app.bot_data["check_job_running"] = True
        app.create_task(_check_refresh_job(app), update=update)


async def _check_refresh_job(application) -> None:
    """Refresh the shared run once, then send each waiting user THEIR OWN fresh result."""
    from . import strategy_service as strategy

    factory = application.bot_data["session_factory"]
    try:
        try:
            result = await strategy.refresh_run(application)
            failure = None if result["status"] == "completed" else "更新没有成功：" + "；".join(result["notes"] or ["行情数据不可用"])
        except strategy.StrategyUnavailable as exc:
            failure = str(exc)
        except Exception as exc:
            log.exception("check refresh failed")
            failure = f"更新失败：{type(exc).__name__}"
        waiters = dict(application.bot_data.get("check_waiters", {}))
        application.bot_data["check_waiters"] = {}
        for user_id, telegram_id in waiters.items():
            try:
                if failure:
                    text = failure + "\n稍后可以再用 /check。"
                else:
                    with transaction(factory) as session:
                        row = strategy.latest_completed(session)
                        text = "✅ <b>已更新</b>\n\n" + strategy.check_text(session, user_id, row, strategy.strategy_settings())
                await application.bot.send_message(telegram_id, text, parse_mode=ParseMode.HTML)
            except Exception:
                log.exception("could not deliver the refreshed check to one user")
    finally:
        application.bot_data["check_job_running"] = False


signals = check


async def cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the caller's own cooldowns only."""
    from . import strategy_service as strategy

    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            blocked, protected = strategy.cooldowns(session, user.id)
    except PermissionError:
        await update.message.reply_text("请先使用 /start 注册。")
        return
    except strategy.StrategyUnavailable as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_text(render_cooldowns(blocked, protected, datetime.now(UTC)), parse_mode=ParseMode.HTML)


def run_started(row) -> datetime:
    return row.started_at if row.started_at.tzinfo else row.started_at.replace(tzinfo=UTC)


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context)
        prefs = session.get(NotificationPreference, user.id)
        if context.args and context.args[0] in {"signals", "announcements"}:
            field = context.args[0] + "_enabled"
            setattr(prefs, field, not getattr(prefs, field))
        text = (f"🔔 <b>通知设置</b>\n每月策略推送：{'开 ✅' if prefs.signals_enabled else '关 ⛔'}\n公告通知：{'开 ✅' if prefs.announcements_enabled else '关 ⛔'}\n\n"
                "切换：/settings signals 或 /settings announcements")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


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
        from . import strategy_service as strategy

        last = strategy.latest_completed(session)
        last_text = f"最近一次完成的策略运行：{last.started_at:%Y-%m-%d %H:%M} UTC，行情日期 {last.trading_date}。" if last else "还没有完成的策略运行。"
    await update.message.reply_text("OK：Telegram 与数据库可用。" + last_text + ("策略每月自动运行已开启。" if context.application.bot_data.get("strategy_autorun") else "策略自动运行未开启（用 /runstrategy 手动运行）。"))


async def run_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
    await update.message.reply_text("开始运行策略（首次可能需要十几分钟）。完成后会通知你，并向开启了信号通知的用户发送建议。")
    context.application.create_task(_run_strategy_job(context.application, update.effective_chat.id), update=update)


async def _run_strategy_job(application, chat_id: int) -> None:
    from . import strategy_service as strategy

    try:
        result = await strategy.run_and_notify(application)
        generation = result.get("generation", {})
        text = (f"策略运行状态：{result['status']}，最后价格日 {result['as_of_bar'] or '无'}，已打分 {result['scored']} 只" + ("（沿用同一天已完成的运行）" if result["reused"] else "") +
                (f"\n用户 {generation['users']} 人，其中 {generation['with_signals']} 人有新建议，出错 {generation['errors']}；已发送 {result['sent']}，失败 {result['failed']}。" if generation else "\n" + "；".join(result["notes"])))
    except strategy.StrategyUnavailable as exc:
        text = str(exc)
    except Exception as exc:
        log.exception("strategy run failed")
        text = f"策略运行失败：{type(exc).__name__}"
    await application.bot.send_message(chat_id, text)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("已取消。", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def configure_application(application: Application, accounts: AccountService, factory) -> Application:
    application.bot_data.update(accounts=accounts, session_factory=factory)
    cancel_handler = CommandHandler("cancel", cancel)
    application.add_handler(ConversationHandler([CommandHandler("start", start)], {CONTACT: [MessageHandler(filters.CONTACT, contact), MessageHandler(filters.ALL & ~filters.COMMAND, contact_reminder)]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("add", add_start)], {ADD_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_data)]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("edit", edit_start)], {EDIT_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_data)], REMOVE_CONFIRM: [CallbackQueryHandler(edit_confirm, pattern=r"^edit:")]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("remove", remove_start)], {REMOVE_CONFIRM: [CallbackQueryHandler(remove_confirm, pattern=r"^remove:")]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("cash", cash_start)], {CASH_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, cash_data)]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("announce", announce_start)], {ANNOUNCE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, announce_text)], ANNOUNCE_CONFIRM: [CallbackQueryHandler(announce_confirm, pattern=r"^announce:")]}, fallbacks=[cancel_handler]))
    for command, handler in [("help", help_command), ("portfolio", portfolio), ("import", import_start), ("export", export_command), ("history", history), ("check", check), ("signals", signals), ("cooldown", cooldown), ("unbind", unbind_start), ("help_import", help_import), ("help_faq", help_faq), ("settings", settings), ("users", users_command), ("stats", stats_command), ("health", health), ("runstrategy", run_strategy)]:
        application.add_handler(CommandHandler(command, handler))
    application.add_handler(CallbackQueryHandler(unbind_confirm, pattern=r"^unbind:"))
    application.add_handler(MessageHandler(filters.Document.FileExtension("csv"), csv_document))
    application.add_error_handler(_error)
    if application.job_queue:
        from .strategy_service import monthly_tick

        application.job_queue.run_repeating(monthly_tick, interval=3600, first=120, name="monthly-strategy")
    return application

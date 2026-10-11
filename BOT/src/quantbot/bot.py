from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    TypeHandler,
    filters,
)

from .allocations import allocation_fraction, parse_weights, save_weights, selections
from .announcements import pending_announcement_ids, publish_announcement
from .chat import (
    create_message,
    deliver_chat_messages,
    purge_expired_chat_messages,
    reply_callback_data,
)
from .csv_io import export_portfolio_csv, import_portfolio_csv
from .db import transaction
from .display import price, qty, render_portfolio_pages
from .help_text import (
    ADMIN_HELP,
    COMMON_FAQ,
    QUICK_NEW,
    QUICK_PENDING,
    STRATEGY1_WELCOME,
    STRATEGY_PICK,
    TOPICS,
    help_for,
    help_pages,
    strategy_management_text,
    welcome_blocks,
)
from .models import (
    AdminGrant,
    AdminPhoneGrant,
    Announcement,
    AnnouncementDelivery,
    ChatMessage,
    NotificationPreference,
    PhoneWhitelist,
    PremiumSubscription,
    SubscriptionPayment,
    Transaction,
    User,
    UserStrategy,
)
from .services import (
    AccountService,
    PortfolioService,
    normalize_exchange,
    normalize_symbol,
    parse_money_text,
    system_stats,
)
from .strategy_text import render_cooldowns

log = logging.getLogger(__name__)
HISTORY_TEXT = {"position_create": "新增持仓", "position_update": "修改持仓", "position_delete": "删除持仓", "cash_set": "设置现金", "cash_adjust": "调整现金",
                "plan_contribution": "定投入账", "csv_import": "导入 CSV"}
CONTACT, ADD_DATA, EDIT_DATA, REMOVE_CONFIRM, CASH_DATA, ANNOUNCE_TEXT, ANNOUNCE_CONFIRM, CHAT_TEXT, CHAT_REPLY_TEXT, REMOVE_DATA = range(10)
EDIT_CONFIRM = 10

def _services(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["accounts"], PortfolioService()


SUBSCRIPTION_STARS = 400
SUBSCRIPTION_SECONDS = 30 * 24 * 60 * 60
ADMIN_CONTACT_URL = "https://t.me/ZhenAi_YongHeng"
SUBSCRIPTION_OFFER = ("需要开通订阅：400 Telegram Stars / 30 天（约 US$7.99，实际价格因购买渠道而异）。"
                      "发送 /subscribe 开通。联系管理员可以有优惠：" + ADMIN_CONTACT_URL)
ADMIN_COMMANDS = {"/admin_help", "/admin_list", "/admin_show", "/admin_add", "/admin_edit", "/admin_remove",
                  "/whitelist_add", "/whitelist_list", "/whitelist_show", "/whitelist_edit", "/whitelist_remove",
                  "/announce", "/important_announce", "/users", "/stats", "/health", "/runstrategy"}

_PUBLIC_MENU = [("start", "开始或查看当前权限"), ("help", "使用说明"),
                ("privacy", "隐私说明"), ("help_faq", "常见问题")]
_REGISTERED_MENU = _PUBLIC_MENU + [("subscribe", "查看或开通订阅"),
                                   ("chat", "给管理员留言"), ("unbind", "解绑账户")]
_STRATEGY_MENU = _REGISTERED_MENU + [
    ("strategy_weights", "查看或分配策略资金权重"),
    ("strategy", "策略管理：启停与资金权重"), ("strategy1", "选择策略 1：加强型 QQQ"),
    ("strategy2", "策略 2：研发中")]
_PREMIUM_MENU = _REGISTERED_MENU + [
    ("strategy_weights", "查看或分配策略资金权重"),
    ("strategy", "策略管理：启停与资金权重"), ("strategy1", "选择策略 1：加强型 QQQ"),
    ("strategy1_off", "停用策略 1"), ("strategy2", "策略 2：研发中"),
    ("check", "查看策略建议"), ("portfolio", "查看我的持仓"),
    ("add", "添加持仓"), ("edit", "修改持仓"), ("remove", "删除持仓"),
    ("cash", "修改现金"), ("import", "导入 CSV"), ("export", "导出 CSV"),
    ("history", "查看操作记录"), ("cooldown", "查看冷却股票"),
    ("signals", "查看最近建议"), ("settings", "通知设置"),
    ("auto_notice_date", "设置每月提醒日期或随机提醒")]
_ADMIN_MENU = _PREMIUM_MENU + [
    ("admin_help", "管理员命令说明书"), ("admin_list", "分页查看管理员"),
    ("admin_show", "查询管理员"), ("admin_add", "添加管理员"),
    ("admin_edit", "修改管理员备注"), ("admin_remove", "移除管理员"),
    ("whitelist_list", "分页查看免费白名单"), ("whitelist_show", "查询白名单"),
    ("whitelist_add", "添加免费白名单"), ("whitelist_edit", "修改白名单备注"),
    ("whitelist_remove", "移除免费白名单"), ("announce", "发布普通公告"),
    ("important_announce", "向所有已验证用户发送并置顶公告"),
    ("users", "查看用户概况"), ("stats", "查看系统统计"),
    ("health", "检查运行状态"), ("runstrategy", "立即运行策略")]


def _menu_for(status: str) -> list[BotCommand]:
    items = (_ADMIN_MENU if status == "admin" else _PREMIUM_MENU if status in {"paid", "whitelist"}
             else _STRATEGY_MENU + _ADMIN_MENU[len(_PREMIUM_MENU):] if status == "admin_strategy_required"
             else _STRATEGY_MENU if status == "strategy_required"
             else _REGISTERED_MENU if status == "subscription_required" else _PUBLIC_MENU)
    return [BotCommand(command, description) for command, description in items]


def _enabled_strategies(session, user_id: str) -> set[str]:
    return set(session.scalars(select(UserStrategy.strategy_id).where(UserStrategy.user_id == user_id)))


def _ui_status(session, accounts: AccountService, telegram_id: int) -> str:
    status = accounts.access_status(session, telegram_id)
    if status in {"admin", "whitelist", "paid"}:
        user = accounts.by_telegram(session, telegram_id)
        if user is None or session.get(UserStrategy, (user.id, "strategy1")) is None:
            return "admin_strategy_required" if status == "admin" else "strategy_required"
    return status


async def _refresh_user_menu(application, telegram_id: int) -> None:
    setter = getattr(getattr(application, "bot", None), "set_my_commands", None)
    if setter is None:
        return
    with transaction(application.bot_data["session_factory"]) as session:
        status = _ui_status(session, application.bot_data["accounts"], telegram_id)
    try:
        await setter(_menu_for(status), scope=BotCommandScopeChat(telegram_id))
    except TelegramError:
        log.warning("could not refresh command menu for user %s", telegram_id, exc_info=True)


async def initialize_command_menus(application: Application) -> None:
    """Publish role-appropriate slash suggestions at startup and for existing users."""
    try:
        await application.bot.set_my_commands(_menu_for("unregistered"),
                                              scope=BotCommandScopeAllPrivateChats())
    except TelegramError:
        log.warning("could not publish default command menu", exc_info=True)
    with transaction(application.bot_data["session_factory"]) as session:
        users = [user.telegram_id for user in session.scalars(select(User).where(User.is_active))]
    for telegram_id in users:
        await _refresh_user_menu(application, telegram_id)
        await asyncio.sleep(0.04)


def _access_text(status: str) -> str:
    if status == "whitelist":
        return "白名单 Premium 已激活（管理员白名单免费授权）。"
    if status == "paid":
        return "付费 Premium 已激活。"
    if status == "admin":
        return "管理员 Premium 已激活。"
    return SUBSCRIPTION_OFFER


def _welcome_text(status: str, strategies: set[str]) -> str:
    access = _access_text(status)
    if status == "subscription_required":
        return access
    if strategies:
        return access + "\n\n" + "\n\n".join(welcome_blocks(strategies)) + ("\n\n/admin_help　管理员命令说明" if status == "admin" else "")
    return access + "\n\n" + STRATEGY_PICK + ("\n\n/admin_help　管理员命令说明" if status == "admin" else "")


async def authorization_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """First handler group: no command, callback, document or payment bypasses access control."""
    # Telegram emits a service message when the bot pins an announcement. It
    # can appear to be from the bot itself; it is not a user registration attempt.
    # Callback queries refer to the bot's original message. Only ignore actual
    # incoming service/bot messages, never the message attached to a callback.
    incoming_message = update.message
    if incoming_message and (getattr(incoming_message, "pinned_message", None) is not None or
                             getattr(getattr(incoming_message, "from_user", None), "is_bot", False)):
        raise ApplicationHandlerStop
    if update.effective_user is None:
        raise ApplicationHandlerStop
    message = update.effective_message
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        status = accounts.access_status(session, update.effective_user.id)
        user = accounts.by_telegram(session, update.effective_user.id)
        enabled_strategy1 = bool(user and session.get(UserStrategy, (user.id, "strategy1")))
    words = (update.message.text or "").split(maxsplit=1) if update.message else []
    command = words[0].split("@", 1)[0].lower() if words else ""
    if command.startswith("/") and command != "/cancel":
        # Every explicit command replaces pending input and confirmation data.
        data = getattr(context, "user_data", {})
        data.clear()
        data["conversation_interrupted"] = True
    if update.pre_checkout_query:
        if status == "unregistered":
            await update.pre_checkout_query.answer(ok=False, error_message="请先使用 /start 验证本人的 Telegram 手机号。")
            raise ApplicationHandlerStop
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        if update.callback_query:
            await update.callback_query.answer("请在与机器人的私聊中操作。", show_alert=True)
        elif message and command:
            await message.reply_text("请在与机器人的私聊中操作。")
        raise ApplicationHandlerStop
    if command in ADMIN_COMMANDS and status != "admin":
        if message:
            await message.reply_text("仅已验证的管理员可使用这个命令。" if status != "unregistered" else
                                     "请先使用 /start 验证本人的 Telegram 手机号。")
        raise ApplicationHandlerStop
    premium = status in {"admin", "whitelist", "paid"}
    if status == "admin":
        data = getattr(context, "user_data", {})
        pending = data.get("pending_command_args", (None,))[0]
        callback = update.callback_query.data or "" if update.callback_query else ""
        if (command in ADMIN_COMMANDS or callback.startswith(("announce:", "chat-reply:")) or
                not command.startswith("/") and not update.callback_query and
                (f"/{pending}" in ADMIN_COMMANDS or "announcement_important" in data or data.get("chat_reply_id"))):
            return
    if premium and enabled_strategy1:
        return
    allowed = {"/start", "/help", "/help_import", "/help_faq", "/privacy", "/cancel"}
    if status != "unregistered":
        allowed.update({"/subscribe", "/unbind", "/chat"})
        if premium:
            allowed.update({"/strategy", "/strategy1", "/strategy1_off", "/strategy2", "/strategy_weights"})
        if message and getattr(message, "successful_payment", None):
            return
        if update.callback_query and (update.callback_query.data or "").startswith("unbind:"):
            return
        if message and message.text and not command.startswith("/") and getattr(context, "user_data", {}).get("chat_allowed"):
            return
    elif message and message.contact:
        return
    if command in allowed:
        return
    if not command.startswith("/") and (getattr(context, "user_data", {}) or {}).get("pending_command_args", (None,))[0] == "help":
        return
    if premium and not command.startswith("/") and (getattr(context, "user_data", {}) or {}).get("pending_command_args", (None,))[0] == "strategy_weights":
        return
    if status == "subscription_required" or premium:
        await _refresh_user_menu(context.application, update.effective_user.id)
    if update.callback_query:
        await update.callback_query.answer("请先验证手机号。" if status == "unregistered" else
                                           "请先开通 Premium。" if not premium else
                                           "此功能属于策略 1，请先发送 /strategy1 启用。", show_alert=True)
    elif message:
        await message.reply_text("请先使用 /start 验证本人的 Telegram 手机号。" if status == "unregistered" else
                                 SUBSCRIPTION_OFFER if not premium else
                                 "此功能属于策略 1，请先启用 /strategy1。用 /help 查看当前已启用策略的说明。")
    raise ApplicationHandlerStop


async def _error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("telegram update failed", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("操作未能正常完成。请先查看当前状态，确认结果后再重试，避免重复提交。")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if getattr(context, "user_data", None) is not None:
        context.user_data.pop("chat_allowed", None)
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        status = accounts.access_status(session, update.effective_user.id)
        user = accounts.by_telegram(session, update.effective_user.id)
        strategies = _enabled_strategies(session, user.id) if user else set()
    await _refresh_user_menu(context.application, update.effective_user.id)
    if status != "unregistered":
        await update.message.reply_text("👋 欢迎回来。\n\n" + _welcome_text(status, strategies),
                                        parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    await update.message.reply_text(BIND_TEXT, reply_markup=_contact_keyboard())
    return CONTACT


BIND_TEXT = (
    "首次使用需要绑定你本人的 Telegram 手机号。请点击下方「分享我的手机号」完成验证。我们默认只保存用于身份匹配的不可逆指纹，不保存完整手机号。授权后会自动检查免费白名单及订阅状态。\n\n"
    "👇 怎么绑定：\n"
    "1. 点输入框下方的按钮「📱 分享我的手机号」。\n"
    "2. 在弹出的确认框里选「分享」。\n\n"
    "没看到按钮？点输入框右边的小键盘图标（或四个点的图标）展开。电脑版/网页版 Telegram 可能不显示这个按钮，请用手机版。\n"
    "注意：文字号码和转发名片不能完成验证，请使用上面的按钮分享你本人的联系人。\n"
    "想先了解这个机器人：/help"
)


def _contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("📱 分享我的手机号", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)


async def contact_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Anything other than the shared contact while waiting for the phone number: say how to do it instead of staying silent."""
    await update.message.reply_text("需要用按钮分享手机号，不能手动输入。\n\n" + BIND_TEXT, reply_markup=_contact_keyboard())
    return CONTACT


async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_chat or update.effective_chat.type != "private":
        return ConversationHandler.END
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        already_bound = accounts.by_telegram(session, update.effective_user.id) is not None
    if already_bound:
        await update.message.reply_text("手机号已经绑定，之前的授权提示已失效。", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    item = update.message.contact
    if (not item or not update.message.from_user or item.user_id != update.message.from_user.id
            or update.message.forward_origin is not None):
        await update.message.reply_text("只能分享当前 Telegram 账号自己的联系人。")
        return CONTACT
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = accounts.register(session, update.effective_user.id, item.phone_number).user
            status = accounts.access_status(session, update.effective_user.id)
            strategies = _enabled_strategies(session, user.id)
    except ValueError as exc:
        text = "这个手机号已经绑定了另一个 Telegram 账号。为防止被冒用，不能自动换绑，请联系管理员核实后处理。" if "already bound" in str(exc) else str(exc)
        await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    await _refresh_user_menu(context.application, update.effective_user.id)
    if getattr(context, "user_data", None) is not None:
        context.user_data.pop("pending_command_args", None)
    await update.message.reply_text(
        "✅ <b>注册完成</b>。\n" + _welcome_text(status, strategies),
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "手机号仅用于本人验证和白名单匹配；数据库默认只保存 HMAC 指纹，不保存完整号码。"
        "付款记录只保存 Telegram 交易标识、金额和到期时间。使用 /unbind 可删除账户及持仓数据。"
    )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        status = accounts.access_status(session, update.effective_user.id)
    if status == "unregistered":
        await update.message.reply_text("请先使用 /start 验证本人的 Telegram 手机号。")
        return
    if status != "subscription_required":
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = accounts.by_telegram(session, update.effective_user.id)
            strategies = _enabled_strategies(session, user.id) if user else set()
        await update.message.reply_text(_welcome_text(status, strategies), parse_mode=ParseMode.HTML)
        return
    link = await context.bot.create_invoice_link(
        title="Premium 订阅（30 天）",
        description="每 30 天自动续订，可访问持仓管理与策略建议。",
        payload=f"premium:{update.effective_user.id}",
        currency="XTR",
        prices=[LabeledPrice("Premium 30 天", SUBSCRIPTION_STARS)],
        provider_token="",
        subscription_period=SUBSCRIPTION_SECONDS,
    )
    await update.message.reply_text(
        SUBSCRIPTION_OFFER,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ 开通 Premium", url=link)],
            [InlineKeyboardButton("💬 联系管理员了解优惠", url=ADMIN_CONTACT_URL)],
        ]),
    )


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        status = accounts.access_status(session, query.from_user.id)
    valid = (status in {"subscription_required", "paid"} and query.invoice_payload == f"premium:{query.from_user.id}"
             and query.currency == "XTR" and query.total_amount == SUBSCRIPTION_STARS)
    await query.answer(ok=valid, error_message=None if valid else "订单或手机号验证状态无效，请重新使用 /subscribe。")


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payment = update.message.successful_payment
    telegram_id = update.effective_user.id
    if (payment.currency != "XTR" or payment.total_amount != SUBSCRIPTION_STARS
            or payment.invoice_payload != f"premium:{telegram_id}" or not payment.telegram_payment_charge_id):
        log.warning("ignored unexpected Stars payment for telegram user %s", telegram_id)
        return
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        if not accounts.is_registered(session, telegram_id):
            return
        expiry = payment.subscription_expiration_date
        if expiry is None:
            user = accounts.by_telegram(session, telegram_id)
            current = session.get(PremiumSubscription, user.id)
            previous = current.expires_at if current else None
            if previous and previous.tzinfo is None:
                previous = previous.replace(tzinfo=UTC)
            expiry = max(datetime.now(UTC), previous or datetime.now(UTC)) + timedelta(seconds=SUBSCRIPTION_SECONDS)
        elif expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        created = accounts.record_subscription_payment(
            session, telegram_id, payment.telegram_payment_charge_id, payment.total_amount, expiry)
    if created:
        await _refresh_user_menu(context.application, telegram_id)
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = accounts.by_telegram(session, telegram_id)
            strategies = _enabled_strategies(session, user.id) if user else set()
        await update.message.reply_text("✅ " + _welcome_text("paid", strategies), parse_mode=ParseMode.HTML)


def _whitelist_phone(args: list[str]) -> str:
    phone = " ".join(args)
    if not re.fullmatch(r"\+\d{1,3} [0-9 ]{6,20}", phone):
        raise ValueError("格式：+(国家区号) (手机号)，例如 /whitelist_add +(国家区号) (手机号)")
    return phone


async def whitelist_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        phone = _whitelist_phone(context.args[:2])
        label = _admin_label(context.args[2:])
        with transaction(context.application.bot_data["session_factory"]) as session:
            created = context.application.bot_data["accounts"].whitelist_add(session, update.effective_user.id, phone, label)
            target = session.scalar(select(User.telegram_id).where(
                User.phone_fingerprint == context.application.bot_data["accounts"].protector.fingerprint(phone),
                User.is_active))
    except (ValueError, PermissionError) as exc:
        await update.message.reply_text(str(exc))
        return
    if created and target is not None:
        await _refresh_user_menu(context.application, target)
    await update.message.reply_text("白名单已添加；已验证该号码的用户立即获得免费 Premium。" if created else "该号码已在白名单中；可用 /whitelist_edit 修改备注。")


async def whitelist_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if len(context.args) != 2:
            raise ValueError("用法：/whitelist_remove +(国家区号) (手机号)")
        phone = _whitelist_phone(context.args)
        with transaction(context.application.bot_data["session_factory"]) as session:
            target = session.scalar(select(User.telegram_id).where(
                User.phone_fingerprint == context.application.bot_data["accounts"].protector.fingerprint(phone),
                User.is_active))
            removed = context.application.bot_data["accounts"].whitelist_remove(session, update.effective_user.id, phone)
    except (ValueError, PermissionError) as exc:
        await update.message.reply_text(str(exc))
        return
    if removed and target is not None:
        await _refresh_user_menu(context.application, target)
    await update.message.reply_text("已从白名单移除；用户其他已付款权益仍然有效。" if removed else "该号码不在白名单中。")


async def whitelist_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) > 1 or (args and (not args[0].isascii() or not args[0].isdecimal() or len(args[0]) > 6)):
        await update.message.reply_text("用法：/whitelist_list [页码]，页码从 1 开始。")
        return
    page = int(args[0]) if args else 1
    if page < 1:
        await update.message.reply_text("页码从 1 开始。")
        return
    accounts, _ = _services(context)
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            accounts.require_maintainer(session, update.effective_user.id)
            rows = list(session.scalars(select(PhoneWhitelist).order_by(PhoneWhitelist.created_at,
                                                                       PhoneWhitelist.phone_fingerprint)))
            bound = {user.phone_fingerprint: user.telegram_id for user in session.scalars(select(User).where(User.is_active))}
    except PermissionError:
        await update.message.reply_text("仅已验证的管理员可查看白名单。")
        return
    size = 10
    pages = max(1, (len(rows) + size - 1) // size)
    if page > pages:
        await update.message.reply_text(f"白名单只有 {pages} 页。")
        return
    lines = []
    for row in rows[(page - 1) * size:page * size]:
        label = f"（{html.escape(row.label)}）" if row.label else ""
        user = f"已绑定 ID <code>{bound[row.phone_fingerprint]}</code>" if row.phone_fingerprint in bound else "尚未绑定"
        lines.append(f"• 指纹 <code>{row.phone_fingerprint[:10]}…</code> {user}{label}")
    body = "\n".join(lines) if lines else "目前没有白名单号码。"
    await update.message.reply_text(
        f"📋 <b>免费 Premium 白名单</b>（第 {page}/{pages} 页，共 {len(rows)} 项）\n{body}\n\n"
        "仅存手机号指纹；用 /whitelist_show +国家区号 手机号 精确查询。",
        parse_mode=ParseMode.HTML,
    )


async def whitelist_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if len(context.args) != 2:
            raise ValueError("用法：/whitelist_show +(国家区号) (手机号)")
        phone = _whitelist_phone(context.args)
        accounts, _ = _services(context)
        with transaction(context.application.bot_data["session_factory"]) as session:
            accounts.require_maintainer(session, update.effective_user.id)
            fingerprint = accounts.protector.fingerprint(phone)
            row = session.get(PhoneWhitelist, fingerprint)
            user = session.scalar(select(User).where(User.phone_fingerprint == fingerprint, User.is_active))
    except (ValueError, PermissionError) as exc:
        await update.message.reply_text(str(exc))
        return
    if row is None:
        await update.message.reply_text("该号码不在白名单中。")
        return
    label = html.escape(row.label) if row.label else "无"
    bound = f"已绑定 ID <code>{user.telegram_id}</code>" if user else "尚未绑定"
    await update.message.reply_text(
        f"白名单：已加入\n手机号指纹：<code>{fingerprint[:10]}…</code>\n账号：{bound}\n备注：{label}",
        parse_mode=ParseMode.HTML,
    )


async def whitelist_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        phone = _whitelist_phone(context.args[:2])
        label = _admin_label(context.args[2:], required=True)
        with transaction(context.application.bot_data["session_factory"]) as session:
            changed = context.application.bot_data["accounts"].whitelist_edit(session, update.effective_user.id,
                                                                                 phone, label)
    except (ValueError, PermissionError) as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_text("白名单备注已更新。" if changed else "该号码不在白名单中。")


def _admin_target(args: list[str]) -> tuple[str, int | str, int]:
    if not args:
        raise ValueError("请提供数字 Telegram ID 或 +国家区号 手机号；用法见 /admin_help。")
    if args[0].startswith("+"):
        if len(args) < 2:
            raise ValueError("手机号格式：+(国家区号) (手机号)。")
        return "phone", _whitelist_phone(args[:2]), 2
    if not args[0].isascii() or not args[0].isdecimal():
        raise ValueError("请提供数字 Telegram ID 或 +国家区号 手机号；用法见 /admin_help。")
    telegram_id = int(args[0])
    if not 0 < telegram_id <= 2**52 - 1:
        raise ValueError("Telegram ID 必须是有效的正整数。")
    return "id", telegram_id, 1


def _admin_label(args: list[str], *, required: bool = False) -> str | None:
    if required and not args:
        raise ValueError("请提供新备注；用 - 清空备注。用法见 /admin_help。")
    label = " ".join(args).strip()
    if label == "-" or not label:
        return None
    if len(label) > 60 or not label.isprintable():
        raise ValueError("备注最多 60 字，不能包含换行或控制字符。")
    return label


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
    except PermissionError:
        await update.message.reply_text("仅已验证的管理员可使用 /admin_help。")
        return
    await update.message.reply_text(ADMIN_HELP, parse_mode=ParseMode.HTML)


async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        page = int(context.args[0]) if context.args else 1
        if page < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("用法：/admin_list [页码]，页码从 1 开始。")
        return
    accounts, _ = _services(context)
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            accounts.require_maintainer(session, update.effective_user.id)
            grants = list(session.scalars(select(AdminGrant).order_by(AdminGrant.telegram_id)))
            phone_grants = list(session.scalars(select(AdminPhoneGrant).order_by(AdminPhoneGrant.created_at)))
            owner_id = accounts.owner_telegram_id
    except PermissionError:
        await update.message.reply_text("仅已验证的管理员可查看管理员名单。")
        return
    if owner_id is None:
        await update.message.reply_text("Bot Owner 尚未在服务器配置。")
        return
    page_size = 10
    start_index = (page - 1) * page_size
    lines = [f"👑 Bot Owner：<code>{owner_id}</code>"] if page == 1 else []
    entries = [("id", grant) for grant in grants] + [("phone", grant) for grant in phone_grants]
    for kind, grant in entries[start_index:start_index + page_size]:
        label = f"（{html.escape(grant.label)}）" if grant.label else ""
        if kind == "id":
            lines.append(f"• ID <code>{grant.telegram_id}</code>{label}")
        else:
            state = f"已绑定 ID <code>{grant.telegram_id}</code>" if grant.telegram_id else "待本人验证"
            lines.append(f"• 手机指纹 <code>{grant.phone_fingerprint[:10]}…</code>：{state}{label}")
    if not lines:
        await update.message.reply_text("这一页没有管理员。")
        return
    pages = max(1, (len(entries) + page_size - 1) // page_size)
    await update.message.reply_text(
        "🛡️ <b>管理员授权名单</b>\n" + "\n".join(lines) + f"\n\nID 授权 {len(grants)} 项，手机号授权 {len(phone_grants)} 项；第 {page}/{pages} 页。",
        parse_mode=ParseMode.HTML,
    )


async def admin_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        kind, target, _ = _admin_target(context.args)
        accounts, _ = _services(context)
        with transaction(context.application.bot_data["session_factory"]) as session:
            accounts.require_maintainer(session, update.effective_user.id)
            if kind == "phone":
                fingerprint = accounts.protector.fingerprint(target)
                phone_grant = session.get(AdminPhoneGrant, fingerprint)
                user = session.scalar(select(User).where(User.phone_fingerprint == fingerprint, User.is_active))
                target_id = user.telegram_id if user else None
            else:
                target_id = target
                user = accounts.by_telegram(session, target_id)
                phone_grant = session.get(AdminPhoneGrant, user.phone_fingerprint) if user else None
            owner = target_id is not None and accounts.is_owner(target_id)
            grant = None if owner or target_id is None else session.get(AdminGrant, target_id)
            verified = target_id is not None and accounts.is_registered(session, target_id)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    except PermissionError:
        await update.message.reply_text("仅已验证的管理员可查询管理员。")
        return
    phone_active = bool(phone_grant and target_id is not None and phone_grant.telegram_id == target_id)
    role = "Bot Owner（不可由命令增删改）" if owner else "管理员" if grant or phone_active else "待验证管理员" if phone_grant and phone_grant.telegram_id is None else "手机号授权已绑定其他账号，需移除后重加" if phone_grant else "非管理员"
    label_value = (phone_grant.label if phone_grant else None) if kind == "phone" else (grant.label if grant else phone_grant.label if phone_grant else None)
    label = f"\n备注：{html.escape(label_value)}" if label_value else ""
    target_text = f"Telegram ID：<code>{target_id}</code>" if target_id is not None else "Telegram ID：尚未绑定"
    if kind == "phone":
        target_text = f"手机号指纹：<code>{fingerprint[:10]}…</code>\n" + target_text
    await update.message.reply_text(
        f"{target_text}\n权限：{role}\n手机号验证：{'已完成' if verified else '未完成'}{label}",
        parse_mode=ParseMode.HTML,
    )


async def admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        kind, target, consumed = _admin_target(context.args)
        label = _admin_label(context.args[consumed:])
        accounts, _ = _services(context)
        with transaction(context.application.bot_data["session_factory"]) as session:
            created = (accounts.admin_add_phone(session, update.effective_user.id, target, label)
                       if kind == "phone" else accounts.admin_add(session, update.effective_user.id, target, label))
            target_id = (session.scalar(select(User.telegram_id).where(
                User.phone_fingerprint == accounts.protector.fingerprint(target), User.is_active))
                if kind == "phone" else target)
            verified = (session.scalar(select(User.id).where(
                User.phone_fingerprint == accounts.protector.fingerprint(target), User.is_active)) is not None
                if kind == "phone" else accounts.is_registered(session, target))
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    except PermissionError as exc:
        await update.message.reply_text(str(exc) if "Owner" in str(exc) else "仅已验证的管理员可添加管理员。")
        return
    if created and verified and target_id is not None:
        await _refresh_user_menu(context.application, target_id)
    await update.message.reply_text(
        ("已添加手机号管理员授权。" if kind == "phone" else f"已添加管理员 <code>{target}</code>。")
        + ("权限已生效。" if verified else "该用户须先用 /start 验证本人手机号，权限才会生效。")
        if created else "该手机号或 Telegram ID 已有对应的管理员授权。", parse_mode=ParseMode.HTML)


async def admin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        kind, target, consumed = _admin_target(context.args)
        label = _admin_label(context.args[consumed:], required=True)
        accounts, _ = _services(context)
        with transaction(context.application.bot_data["session_factory"]) as session:
            changed = (accounts.admin_edit_phone(session, update.effective_user.id, target, label)
                       if kind == "phone" else accounts.admin_edit(session, update.effective_user.id, target, label))
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    except PermissionError as exc:
        await update.message.reply_text(str(exc) if "Owner" in str(exc) else "仅已验证的管理员可修改管理员。")
        return
    await update.message.reply_text("管理员备注已更新。" if changed else "该手机号或 Telegram ID 没有对应的管理员授权。")


async def admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        kind, target, _ = _admin_target(context.args)
        accounts, _ = _services(context)
        with transaction(context.application.bot_data["session_factory"]) as session:
            target_id = (session.scalar(select(User.telegram_id).where(
                User.phone_fingerprint == accounts.protector.fingerprint(target), User.is_active))
                if kind == "phone" else target)
            removed = (accounts.admin_remove_phone(session, update.effective_user.id, target)
                       if kind == "phone" else accounts.admin_remove(session, update.effective_user.id, target))
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    except PermissionError as exc:
        await update.message.reply_text(str(exc) if "Owner" in str(exc) else "仅已验证的管理员可移除管理员。")
        return
    if removed and target_id is not None:
        await _refresh_user_menu(context.application, target_id)
    await update.message.reply_text("这项管理员授权已移除；如有另一项授权，其权限仍有效。" if removed else "该手机号或 Telegram ID 没有对应的管理员授权。")


async def help_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.args = ["导入"]
    await help_command(update, context)


async def help_faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.args = ["问题"]
    await help_command(update, context)


async def unbind_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        try:
            user = context.application.bot_data["accounts"].require(session, update.effective_user.id)
            can_export = context.application.bot_data["accounts"].has_premium_access(session, update.effective_user.id)
        except PermissionError:
            await update.message.reply_text("你还没有绑定，不需要解绑。发 /start 开始绑定。")
            return
    nonce = secrets.token_urlsafe(12)
    context.user_data["unbind_confirmation"] = (user.id, nonce)
    await update.message.reply_text(
        "⚠️ 解绑会删除你在这个机器人里的全部数据：手机号绑定、持仓、现金、操作记录和建议。不能撤销。\n"
        + ("建议先用 /export 导出你的持仓。" if can_export else
           "当前无 Premium 权限，无法导出持仓；如需保留数据，请先恢复权限再用 /export 导出。")
        + "解绑后可以随时用 /start 重新绑定（同一个或另一个手机号）。\n\n确定要解绑吗？",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("确认解绑并删除所有数据", callback_data=f"unbind:yes:{user.id}:{nonce}"),
            InlineKeyboardButton("取消", callback_data=f"unbind:no:{user.id}:{nonce}"),
        ]]),
    )


async def unbind_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes only the account of whoever pressed the button (never anyone else's)."""
    query = update.callback_query
    match = re.fullmatch(r"unbind:(yes|no):([0-9a-f-]{36}):([A-Za-z0-9_-]{16})", query.data or "")
    saved = context.user_data.get("unbind_confirmation")
    if not match or saved != (match.group(2), match.group(3)):
        await query.answer("这个确认按钮已失效，请重新发送 /unbind。", show_alert=True)
        return
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = accounts.by_telegram(session, update.effective_user.id)
        current_id = user.id if user else None
        subscription = session.get(PremiumSubscription, user.id) if user and user.id == match.group(2) else None
        charge_id = (session.scalar(select(SubscriptionPayment.telegram_charge_id)
                                    .where(SubscriptionPayment.user_id == user.id)
                                    .order_by(SubscriptionPayment.paid_at, SubscriptionPayment.telegram_charge_id)
                                    .limit(1)) if user and user.id == match.group(2) and subscription and
                     (subscription.expires_at if subscription.expires_at.tzinfo else
                      subscription.expires_at.replace(tzinfo=UTC)) > datetime.now(UTC) else None)
    if current_id != match.group(2):
        context.user_data.pop("unbind_confirmation", None)
        await query.answer("这个确认按钮已失效，请重新发送 /unbind。", show_alert=True)
        return
    if match.group(1) == "no":
        context.user_data.pop("unbind_confirmation", None)
        await query.answer()
        await query.edit_message_text("已取消，什么都没有改变。")
        return
    await query.answer()
    if charge_id:
        try:
            canceled = await context.application.bot.edit_user_star_subscription(
                update.effective_user.id, charge_id, is_canceled=True)
        except TelegramError:
            log.exception("could not cancel Stars subscription before unbinding")
            await query.edit_message_text("订阅自动续费暂时无法取消，账户未删除。请稍后重试或联系管理员。")
            return
        if not canceled:
            await query.edit_message_text("订阅自动续费暂时无法取消，账户未删除。请稍后重试或联系管理员。")
            return
    with transaction(context.application.bot_data["session_factory"]) as session:
        current = accounts.by_telegram(session, update.effective_user.id)
        done = bool(current and current.id == match.group(2) and
                    context.user_data.get("unbind_confirmation") == saved and
                    accounts.unregister(session, update.effective_user.id))
    context.user_data.clear()
    waiters = context.application.bot_data.get("check_waiters", {})
    for key in [k for k, telegram_id in waiters.items() if telegram_id == update.effective_user.id]:
        del waiters[key]
    if done:
        log.info("a user unbound and their data was deleted")
        await _refresh_user_menu(context.application, update.effective_user.id)
    await query.edit_message_text("已解绑，你的全部数据已删除。需要时发 /start 重新绑定。" if done else "你还没有绑定。发 /start 开始绑定。")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        status = accounts.access_status(session, update.effective_user.id)
        user = accounts.by_telegram(session, update.effective_user.id)
        enabled = _enabled_strategies(session, user.id) if user else set()
    if status in {"admin", "whitelist", "paid"} and (not context.args or context.args[0].lower() not in TOPICS):
        for page in help_pages(enabled, admin=status == "admin"):
            await update.message.reply_text(page, parse_mode=ParseMode.HTML)
        return
    if status in {"admin", "whitelist", "paid"} and context.args and "strategy1" not in enabled:
        if context.args[0].lower() in {"问题", "faq"}:
            await update.message.reply_text(COMMON_FAQ, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("这个帮助主题介绍策略 1 的功能，你尚未启用策略 1。"
                                            "请用 /help 查看当前策略的说明，或用 /strategy1 启用策略 1。")
        return
    text = (QUICK_NEW if status == "unregistered" and not context.args else
            QUICK_PENDING if status == "subscription_required" and not context.args else
            help_for(context.args[0] if context.args else None, True))
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def _require(session, update, context, *, allow_unselected: bool = False):
    accounts = context.application.bot_data["accounts"]
    user = accounts.require(session, update.effective_user.id)
    if context.application.bot_data.get("enforce_premium") and not accounts.has_premium_access(session, update.effective_user.id):
        raise PermissionError("Premium access required")
    if not allow_unselected and session.get(UserStrategy, (user.id, "strategy1")) is None:
        raise PermissionError("Strategy selection required")
    return user


async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) > 1 or (context.args and
            (not context.args[0].isascii() or not context.args[0].isdecimal() or len(context.args[0]) > 6)):
        await update.message.reply_text("用法：/portfolio [页码]，页码从 1 开始。")
        return
    page = int(context.args[0]) if context.args else 1
    if page < 1:
        await update.message.reply_text("页码从 1 开始。")
        return
    _, portfolios = _services(context)
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            cash = portfolios.cash(session, user.id)
            positions = portfolios.list_positions(session, user.id)
            pages = render_portfolio_pages(cash, positions)
            account_id = user.id
    except PermissionError:
        await update.message.reply_text("请先使用 /start 注册。")
        return
    if page > len(pages):
        await update.message.reply_text(f"持仓只有 {len(pages)} 页，请使用 /portfolio 1 查看。")
        return
    await update.message.reply_text(pages[page - 1], parse_mode=ParseMode.HTML,
                                    reply_markup=_portfolio_navigation(account_id, page, len(pages)))


def _portfolio_navigation(account_id: str, page: int, total: int) -> InlineKeyboardMarkup | None:
    if total == 1:
        return None
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("◀ 上一页", callback_data=f"portfolio:{account_id}:{page - 1}"))
    if page < total:
        buttons.append(InlineKeyboardButton("下一页 ▶", callback_data=f"portfolio:{account_id}:{page + 1}"))
    return InlineKeyboardMarkup([buttons])


async def portfolio_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = re.fullmatch(r"portfolio:([0-9a-f-]{36}):([1-9][0-9]{0,5})", query.data or "")
    if match is None:
        await query.answer("页面已失效，请重新发送 /portfolio。", show_alert=True)
        return
    with transaction(context.application.bot_data["session_factory"]) as session:
        try:
            user = _require(session, update, context)
        except PermissionError:
            await query.answer("请先完成验证并开通 Premium。", show_alert=True)
            return
        if user.id != match.group(1):
            await query.answer("这是旧账户的页面，请重新发送 /portfolio。", show_alert=True)
            return
        portfolios = PortfolioService()
        pages = render_portfolio_pages(portfolios.cash(session, user.id),
                                       portfolios.list_positions(session, user.id))
        account_id = user.id
    page = int(match.group(2))
    if page > len(pages):
        await query.answer("页数已变化，请重新发送 /portfolio。", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(pages[page - 1], parse_mode=ParseMode.HTML,
                                  reply_markup=_portfolio_navigation(account_id, page, len(pages)))


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("添加一只当前持有的股票。下一条发送：股票代码 股数 平均成本 币种 [交易所]\n例如：ISRG 0.55 374.21 USD\n股数支持小数，平均成本是每股成本；交易所可省略，默认 US。这里只记录持仓，不会下单或扣减现金。取消请发 /cancel。")
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
    await update.message.reply_text(f"已添加 {symbol.upper()}。可继续 /add，或用 /portfolio 核对。实际买入后请同时用 /cash 更新剩余现金。")
    return ConversationHandler.END


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("下一条发送：股票代码 新股数 新平均成本 [交易所]\n例如：ISRG 0.75 380 US\n填写修改后的总股数，不是本次买卖股数；全部卖出请用 /remove。更新持仓不会自动调整现金。取消请发 /cancel。")
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
    return EDIT_CONFIRM


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
                                           exchange=exchange, actor=update.effective_user.id, record_sale=True)
        prompt, markup = _sale_cooldown_prompt(session, user.id)
    await query.edit_message_text(f"已更新 {symbol.upper()}。" + prompt, reply_markup=markup)
    return ConversationHandler.END


async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.args:
        await update.message.reply_text("请在下一条消息输入股票代码 [交易所]，例如 ISRG US。取消请发 /cancel。")
        return REMOVE_DATA
    try:
        if len(context.args) not in (1, 2):
            raise ValueError("invalid number of arguments")
        normalize_symbol(context.args[0])
        if len(context.args) == 2:
            normalize_exchange(context.args[1])
    except ValueError:
        await update.message.reply_text("格式不正确，请输入股票代码 [交易所]，例如 ISRG US。取消请发 /cancel。")
        return REMOVE_DATA
    symbol, exchange = context.args[0], context.args[1] if len(context.args) > 1 else "US"
    context.user_data["remove"] = (symbol, exchange)
    await update.message.reply_text(f"确认删除 {symbol.upper()}@{exchange.upper()}？", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("确认删除", callback_data="remove:yes"), InlineKeyboardButton("取消", callback_data="remove:no")]]))
    return REMOVE_CONFIRM


async def remove_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.args = update.message.text.split()
    return await remove_start(update, context)


async def remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "remove:no":
        await query.edit_message_text("已取消。")
        return ConversationHandler.END
    symbol, exchange = context.user_data.pop("remove")
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context)
        PortfolioService().remove_position(session, user, symbol, exchange, actor=update.effective_user.id, record_sale=True)
        prompt, markup = _sale_cooldown_prompt(session, user.id)
    await query.edit_message_text(f"已删除 {symbol.upper()}。" + prompt, reply_markup=markup)
    return ConversationHandler.END


def _sale_cooldown_prompt(session, user_id):
    session.flush()
    row = session.scalar(select(Transaction).where(Transaction.user_id == user_id)
                         .order_by(Transaction.id.desc()).limit(1))
    if row is None or "sale_cooldown" not in (row.after_data or {}):
        return "", None
    return (("\n\n要把卖出的股票加入冷却期吗？（默认设置：加入）\n"
             "已默认加入；如果只是修改记录、没有实际卖出，请选择「不加入」。"),
            InlineKeyboardMarkup([[
                InlineKeyboardButton("加入（默认）", callback_data=f"sale-cooldown:{row.id}:yes"),
                InlineKeyboardButton("不加入", callback_data=f"sale-cooldown:{row.id}:no"),
            ]]))


async def sale_cooldown_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, row_id, choice = query.data.split(":")
    with transaction(context.application.bot_data["session_factory"]) as session:
        try:
            user = _require(session, update, context)
        except PermissionError:
            await query.answer("请先使用 /start 注册。", show_alert=True)
            return
        row = session.get(Transaction, int(row_id))
        setting = (row.after_data or {}).get("sale_cooldown") if row and row.user_id == user.id else None
        if setting is None:
            await query.answer("这条记录不可用。", show_alert=True)
            return
        if not setting["answered"]:
            setting = {"enabled": choice == "yes", "answered": True}
            row.after_data = {**row.after_data, "sale_cooldown": setting}
        text = (f"已将 {row.symbol} 加入冷却期。" if setting["enabled"] else
                "本次卖出记录不加入冷却期；持仓修改已保存，其他卖出记录产生的冷却期不受影响。")
    await query.answer()
    await query.edit_message_text(text)


async def cash_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("请输入当前可投资现金余额，例如：1000 USD（也可以写 1000usd、$1000、1,000.5 美元；不写币种按美元）。这会替换原余额，不是充值，也不是在原余额上累加；不要包含股票市值。策略只分析美元金额。取消请发 /cancel。")
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
    await update.message.reply_text("请发送 UTF-8 CSV 文件（不超过 1 MB），格式见 /help_import。现有同名持仓不会被覆盖；文件中包含已有股票会拒绝整次导入。current_cash 行会替换现金余额。取消请发 /cancel。")


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
    await update.message.reply_text(f"导入完成：{result.positions_created} 条持仓。请用 /portfolio 核对持仓和现金，再用 /check 查看建议。")


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


async def strategy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await update.message.reply_text("/strategy 不需要参数，请单独发送打开策略管理，再选择页面里的操作命令。")
        return
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context, allow_unselected=True)
        rows = selections(session, user.id)
        weights = {row.strategy_id: row.weight_percent for row in rows}
        try:
            allocation_fraction(session, user.id, rows[0].strategy_id if rows else "strategy1")
            valid = True
        except ValueError:
            valid = False
    await update.message.reply_text(strategy_management_text(weights, valid))


async def strategy_weights(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            user = _require(session, update, context, allow_unselected=True)
            if context.args:
                save_weights(session, user.id, parse_weights(context.args))
                session.flush()
            rows = selections(session, user.id)
            lines = [f"{row.strategy_id.removeprefix('strategy')}：{row.weight_percent}%" if row.weight_percent is not None
                     else f"{row.strategy_id.removeprefix('strategy')}：待分配" for row in rows]
            try:
                allocation_fraction(session, user.id, rows[0].strategy_id if rows else "strategy1")
                status = "分配已生效。"
            except ValueError as exc:
                status = str(exc)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_text("📊 策略资金权重\n" + ("\n".join(lines) or "尚未启用策略，请先 /strategy。") +
                                    "\n\n" + status + "\n按持仓市值与现金的合计分配，每项大于 0，总和为 100%。"
                                    "目前只有策略 1 可用，填写 1=100。未来多策略示例：1=60 2=40。"
                                    "权重用于计算建议，不会替你转账或下单；修改不会补发已处理月份的通知。\n/strategy　返回策略管理")


async def strategy_one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await update.message.reply_text("/strategy1 不需要参数，请单独发送。")
        return
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context, allow_unselected=True)
        if session.get(UserStrategy, (user.id, "strategy1")) is None:
            session.add(UserStrategy(user_id=user.id, strategy_id="strategy1"))
    await _refresh_user_menu(context.application, update.effective_user.id)
    await update.message.reply_text("✅ 已启用策略 1。\n\n" + STRATEGY1_WELCOME, parse_mode=ParseMode.HTML)


async def strategy_one_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await update.message.reply_text("/strategy1_off 不需要参数，请单独发送。")
        return
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context, allow_unselected=True)
        selection = session.get(UserStrategy, (user.id, "strategy1"))
        if selection is not None:
            session.delete(selection)
    if selection is None:
        await update.message.reply_text("你尚未启用策略 1，无需停用。发送 /strategy1 即可启用。")
        return
    await _refresh_user_menu(context.application, update.effective_user.id)
    await update.message.reply_text("策略 1 已停用，不会再向你推送策略 1 的通知。持仓、现金和历史记录保留，不会自动卖出。"
                                    "如果还启用了其他策略，请用 /strategy_weights 重新分配到 100%。")
    await strategy_command(update, context)


async def strategy_two(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await update.message.reply_text("/strategy2 不需要参数，请单独发送。")
        return
    with transaction(context.application.bot_data["session_factory"]) as session:
        _require(session, update, context, allow_unselected=True)
    await update.message.reply_text("策略 2 正在研发中，暂不可选。你当前的策略没有改变。\n/strategy　返回策略管理")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Today's scores and suggestions for the CALLER only (own portfolio, own D scores). If the shared data is older than 12 hours it is shown with a notice while a refresh runs in the
    background; the caller then gets the fresh result automatically. Suggestions only: nothing is ordered."""
    from . import strategy_service as strategy

    app = context.application
    try:
        with transaction(app.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            allocation_fraction(session, user.id)
            settings = strategy.strategy_settings()
            row = strategy.latest_completed(session)
            fresh = row is not None and strategy.is_fresh(row) and strategy.covers_holdings(session, user.id, row)
            text = strategy.check_text(session, user.id, row, settings) if row is not None else ""
            recent = None if fresh else strategy.recent_attempt(session)
            user_id = user.id
    except PermissionError:
        await update.message.reply_text("请先使用 /start 注册。")
        return
    except (strategy.StrategyUnavailable, ValueError) as exc:
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
                with transaction(factory) as session:
                    user = application.bot_data["accounts"].by_telegram(session, telegram_id)
                    if (user is None or user.id != user_id or
                            session.get(UserStrategy, (user.id, "strategy1")) is None or
                            application.bot_data.get("enforce_premium") and
                            not application.bot_data["accounts"].has_premium_access(session, telegram_id)):
                        continue
                if failure:
                    text = "策略 1 · " + failure + "\n稍后可以再用 /check。"
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
    await update.message.reply_text("策略 1：加强型 QQQ\n" + render_cooldowns(blocked, protected, datetime.now(UTC)), parse_mode=ParseMode.HTML)


def run_started(row) -> datetime:
    return row.started_at if row.started_at.tzinfo else row.started_at.replace(tzinfo=UTC)


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        user = _require(session, update, context)
        prefs = session.get(NotificationPreference, user.id)
        if context.args and context.args[0] in {"signals", "announcements"}:
            field = context.args[0] + "_enabled"
            setattr(prefs, field, not getattr(prefs, field))
        schedule = ("每月随机一个美股交易日" if prefs.auto_notice_day == 0 else
                    f"每月第 {prefs.auto_notice_day} 个美股交易日")
        text = (f"🔔 <b>通知设置</b>\n策略 1 每月推送：{'开 ✅' if prefs.signals_enabled else '关 ⛔'}\n公告通知（账户通用）：{'开 ✅' if prefs.announcements_enabled else '关 ⛔'}\n\n"
                f"自动通知日期：{schedule}\n\n"
                "切换：/settings signals 或 /settings announcements\n修改日期：/auto_notice_date 5；随机：/auto_notice_date random")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def chat_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("chat_cancelled_by_command", None)
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            accounts = context.application.bot_data["accounts"]
            accounts.require(session, update.effective_user.id)
            maintainers = accounts.maintainer_ids(session)
    except PermissionError:
        await update.message.reply_text("请先使用 /start 绑定手机号，之后才能给管理员留言。")
        return ConversationHandler.END
    if not maintainers:
        await update.message.reply_text("管理员留言功能暂未配置，请稍后再试。")
        return ConversationHandler.END
    context.user_data["chat_allowed"] = True
    await update.message.reply_text("请输入要发给管理员的留言（最多 2000 字）。取消请发 /cancel。")
    return CHAT_TEXT


async def chat_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.pop("chat_cancelled_by_command", False):
        await update.message.reply_text("之前的留言输入已取消。如需留言，请重新发送 /chat。")
        return ConversationHandler.END
    body = (update.message.text or "").strip()
    if not body:
        await update.message.reply_text("留言不能为空，请重新输入。")
        return CHAT_TEXT
    if len(body) > 2000:
        await update.message.reply_text(f"留言不能超过 2000 字（当前 {len(body)} 字），请缩短后重新发送。")
        return CHAT_TEXT
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            accounts = context.application.bot_data["accounts"]
            user = accounts.require(session, update.effective_user.id)
            maintainers = accounts.maintainer_ids(session)
            if not maintainers:
                raise LookupError("管理员留言功能暂未配置，留言没有提交。")
            message = create_message(session, user.id, body, maintainers)
            message_id = message.id
    except PermissionError:
        context.user_data.pop("chat_allowed", None)
        await update.message.reply_text("你的绑定已失效，请重新使用 /start。")
        return ConversationHandler.END
    except LookupError as exc:
        context.user_data.pop("chat_allowed", None)
        await update.message.reply_text(str(exc))
        return ConversationHandler.END
    sent, failed = await deliver_chat_messages(context.application, message_id)
    if sent:
        await update.message.reply_text(f"✅ 留言 #{message_id} 已发送给管理员，将保存 14 天。管理员回复后会通过机器人发给你。")
    else:
        await update.message.reply_text(f"📮 留言 #{message_id} 已保存 14 天，暂时未能通知管理员；系统会自动重试。")
    if failed:
        log.warning("chat message %s: %s maintainer deliveries failed", message_id, failed)
    context.user_data.pop("chat_allowed", None)
    return ConversationHandler.END


async def chat_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data.pop("pending_command_args", None)
    context.user_data.pop("chat_reply_id", None)
    context.user_data.pop("chat_reply_callback", None)
    match = re.fullmatch(r"chat-reply:(\d+):(\d{20})", query.data or "")
    purge_expired_chat_messages(context.application.bot_data["session_factory"])
    try:
        with transaction(context.application.bot_data["session_factory"]) as session:
            context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
            message = session.get(ChatMessage, int(match.group(1))) if match else None
            if message is None or query.data != reply_callback_data(message):
                text = "这条留言已不存在，可能已经超过 14 天或用户已经解绑。"
            elif message.replied_at:
                text = "这条留言已经回复过了。"
            else:
                context.user_data["chat_reply_id"] = message.id
                context.user_data["chat_reply_callback"] = query.data
                text = f"请输入对留言 #{message.id} 的回复（最多 2000 字）。取消请发 /cancel。"
    except PermissionError:
        await query.answer("仅管理员可以回复。", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text(text)
    return CHAT_REPLY_TEXT if "chat_reply_id" in context.user_data else ConversationHandler.END


async def chat_reply_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    body = (update.message.text or "").strip()
    if not body:
        await update.message.reply_text("回复不能为空，请重新输入。")
        return CHAT_REPLY_TEXT
    if len(body) > 2000:
        await update.message.reply_text(f"回复不能超过 2000 字（当前 {len(body)} 字），请缩短后重新发送。")
        return CHAT_REPLY_TEXT
    message_id = context.user_data.get("chat_reply_id")
    callback_data = context.user_data.get("chat_reply_callback")
    if not message_id or not callback_data:
        await update.message.reply_text("回复目标已失效，请从留言通知中重新点“回复用户”。")
        return ConversationHandler.END
    lock = context.application.bot_data.setdefault("chat_reply_lock", asyncio.Lock())
    async with lock:
        purge_expired_chat_messages(context.application.bot_data["session_factory"])
        try:
            with transaction(context.application.bot_data["session_factory"]) as session:
                context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
                message = session.get(ChatMessage, message_id)
                if message is None or reply_callback_data(message) != callback_data:
                    await update.message.reply_text("这条留言已不存在，可能已经超过 14 天或用户已经解绑。")
                    context.user_data.pop("chat_reply_id", None)
                    context.user_data.pop("chat_reply_callback", None)
                    return ConversationHandler.END
                if message.replied_at:
                    await update.message.reply_text("这条留言已经被其他管理员回复。")
                    context.user_data.pop("chat_reply_id", None)
                    context.user_data.pop("chat_reply_callback", None)
                    return ConversationHandler.END
                user = session.get(User, message.user_id)
                if user is None or not user.is_active:
                    await update.message.reply_text("用户已经解绑，无法发送回复。")
                    context.user_data.pop("chat_reply_id", None)
                    context.user_data.pop("chat_reply_callback", None)
                    return ConversationHandler.END
                telegram_id = user.telegram_id
            try:
                await context.application.bot.send_message(
                    telegram_id, f"💬 管理员回复你的留言 #{message_id}\n\n{body}")
            except TelegramError:
                log.exception("could not deliver maintainer reply")
                await update.message.reply_text("回复暂时发送失败，留言仍未标记为已回复。请重新发送，或用 /cancel 取消。")
                return CHAT_REPLY_TEXT
            with transaction(context.application.bot_data["session_factory"]) as session:
                message = session.get(ChatMessage, message_id)
                if message is not None and reply_callback_data(message) == callback_data and message.replied_at is None:
                    message.replied_by_telegram_id = update.effective_user.id
                    message.reply_body = body
                    message.replied_at = datetime.now(UTC)
        except PermissionError:
            await update.message.reply_text("仅管理员可以回复。")
            context.user_data.pop("chat_reply_id", None)
            context.user_data.pop("chat_reply_callback", None)
            return ConversationHandler.END
    context.user_data.pop("chat_reply_id", None)
    context.user_data.pop("chat_reply_callback", None)
    await update.message.reply_text(f"✅ 已回复留言 #{message_id}。")
    return ConversationHandler.END


async def auto_notice_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from . import strategy_service as strategy

    app = context.application
    try:
        if len(context.args) > 1:
            raise ValueError("只接受一个参数，例如 /auto_notice_date 5 或 /auto_notice_date random。设置未修改。")
        day = (0 if context.args and context.args[0].lower() in {"random", "随机"} else
               strategy.parse_notice_day(context.args[0]) if context.args else None)
        tz, hour = app.bot_data.get("strategy_push", strategy.DEFAULT_PUSH)
        today = datetime.now(ZoneInfo(tz)).date()
        with transaction(app.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            prefs = session.get(NotificationPreference, user.id)
            if day is not None:
                if day == 0 and prefs.auto_notice_day != 0:
                    prefs.random_notice_started_on = today
                elif day != 0:
                    prefs.random_notice_started_on = None
                prefs.auto_notice_day = day
            day, completed, enabled, started_on = (prefs.auto_notice_day, prefs.last_notice_month,
                                                   prefs.signals_enabled, prefs.random_notice_started_on)
            user_id = user.id
    except PermissionError:
        await update.message.reply_text("请先使用 /start 注册。")
        return
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    if day == 0:
        text = (f"策略 1 · {'已设置' if context.args else '当前设置'}：每月随机选一个美股交易日通知，只影响你自己。\n"
                f"通知时间：{tz} {hour:02d}:00 后的定时检查。\n"
                "用法：/auto_notice_date random；改回固定日期：/auto_notice_date 5。\n"
                "一个月最多通知一次；当月已通知过不会重复通知。")
        try:
            text += "\n\n" + await asyncio.to_thread(
                strategy.random_notice_preview, strategy.strategy_settings(), today, user_id,
                app.bot_data["accounts"].protector, started_on, completed)
        except strategy.StrategyUnavailable:
            text += "\n本月、下个月：交易日历暂不可用，尚未确认。"
        if not enabled:
            text += "\n你的策略通知目前已关闭，可用 /settings signals 开启。"
        if not app.bot_data.get("strategy_autorun"):
            text += "\n系统自动推送尚未开启；设置已保存，管理员开启后生效。"
        await update.message.reply_text(text)
        return
    text = (f"{'已设置' if context.args else '当前设置'}：每月第 {day} 个美股交易日通知，只影响你自己。\n"
            f"通知时间：{tz} {hour:02d}:00 后的定时检查。\n"
            "用法：/auto_notice_date 5；随机：/auto_notice_date random；不带参数查看设置。\n"
            "交易日不含周末及休市日。当月交易日不足则跳过，不顺延、不提前；当月已通知过也不会重复通知。")
    if day >= 19:
        text += f"\n⚠️ 日期较靠后，2 月等短月份可能没有第 {day} 个交易日。"
    if day >= 22:
        text += f"\n⚠️ 2 月即使是闰年也最多只有 21 个工作日，因此不会有第 {day} 个交易日通知。"
    try:
        text += "\n\n" + await asyncio.to_thread(strategy.notice_date_preview, strategy.strategy_settings(), today, day, completed)
    except strategy.StrategyUnavailable:
        text += "\n本月、下个月及后续月份：交易日历暂不可用，尚未确认。设置已保留，取得真实交易日历后才能确认。"
    if not enabled:
        text += "\n你的策略通知目前已关闭，可用 /settings signals 开启。"
    if not app.bot_data.get("strategy_autorun"):
        text += "\n系统自动推送尚未开启；设置已保存，管理员开启后生效。"
    await update.message.reply_text("策略 1 · 提醒日期\n" + text)


async def announce_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    with transaction(context.application.bot_data["session_factory"]) as session:
        context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
    important = update.message.text.split(maxsplit=1)[0].split("@", 1)[0].lower() == "/important_announce"
    context.user_data["announcement_important"] = important
    context.user_data.pop("announcement", None)
    context.user_data.pop("announcement_nonce", None)
    await update.message.reply_text(
        "请输入重要公告正文。确认后会发送给所有已验证用户（包括关闭普通公告通知或尚未开通 Premium 的用户），逐一置顶，并替换上一条重要公告置顶。"
        if important else "请输入公告正文。")
    return ANNOUNCE_TEXT


async def announce_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if len(update.message.text) > 3500:
        await update.message.reply_text("公告不能超过 3500 字。")
        return ANNOUNCE_TEXT
    context.user_data["announcement"] = update.message.text
    nonce = secrets.token_urlsafe(12)
    context.user_data["announcement_nonce"] = nonce
    important = context.user_data.get("announcement_important", False)
    preview = "重要公告预览（发送给所有已验证用户并替换旧置顶）：\n\n" if important else "预览：\n\n"
    await update.message.reply_text(preview + update.message.text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("确认发送并置顶" if important else "确认发布", callback_data=f"announce:yes:{nonce}"), InlineKeyboardButton("取消", callback_data=f"announce:no:{nonce}")]]))
    return ANNOUNCE_CONFIRM


async def announce_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    match = re.fullmatch(r"announce:(yes|no):([A-Za-z0-9_-]{16})", query.data or "")
    if not match or context.user_data.get("announcement_nonce") != match.group(2):
        await query.answer("这个确认按钮已失效，请重新输入命令。", show_alert=True)
        return ANNOUNCE_CONFIRM
    await query.answer()
    context.user_data.pop("announcement_nonce", None)
    if match.group(1) == "no":
        context.user_data.pop("announcement", None)
        context.user_data.pop("announcement_important", None)
        await query.edit_message_text("已取消。")
        return ConversationHandler.END
    content = context.user_data.pop("announcement", None)
    important = context.user_data.pop("announcement_important", False)
    if content is None:
        await query.edit_message_text("这条公告预览已失效，请重新输入命令。")
        return ConversationHandler.END
    with transaction(context.application.bot_data["session_factory"]) as session:
        context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
        item = Announcement(content=content, important=important,
                            created_by_telegram_id=update.effective_user.id)
        session.add(item)
        session.flush()
        announcement_id = item.id
    await query.edit_message_text("重要公告已保存，开始向所有已验证用户发送并置顶。完成后会通知你。" if important else
                                  "公告已保存，开始后台推送。完成后会通知你。")
    context.application.create_task(
        _publish_announcement_job(context.application, announcement_id, update.effective_user.id), update=update)
    return ConversationHandler.END


async def _publish_announcement_job(application, announcement_id: int, admin_id: int) -> None:
    try:
        await publish_announcement(application, announcement_id)
        with transaction(application.bot_data["session_factory"]) as session:
            item = session.get(Announcement, announcement_id)
            deliveries = list(session.scalars(select(AnnouncementDelivery).where(
                AnnouncementDelivery.announcement_id == announcement_id)))
            done = sum(bool(row.pinned_at if item.important else row.delivered_at) for row in deliveries)
        result = (f"{'重要' if item.important else '普通'}公告 #{announcement_id} 处理完成："
                  f"{done}/{len(deliveries)} 位用户{'已发送并置顶' if item.important else '已发送'}。")
        if done < len(deliveries):
            result += "部分用户无法接收或置顶；失败记录已保存。"
    except Exception:
        log.exception("announcement %s failed", announcement_id)
        result = f"公告 #{announcement_id} 推送发生异常，请检查服务日志。"
    try:
        await application.bot.send_message(admin_id, result)
    except TelegramError:
        log.warning("could not deliver announcement result to admin %s", admin_id, exc_info=True)


async def resume_announcements(context) -> None:
    """Resume confirmed publications after restart, including unfinished pinning."""
    app = context.application
    lock = app.bot_data.setdefault("announcement_resume_lock", asyncio.Lock())
    publish_lock = app.bot_data.get("announcement_publish_lock")
    if lock.locked() or publish_lock and publish_lock.locked():
        return
    async with lock:
        for announcement_id in pending_announcement_ids(app.bot_data["session_factory"]):
            with transaction(app.bot_data["session_factory"]) as session:
                item = session.get(Announcement, announcement_id)
                if item is None:
                    continue
                admin_id = item.created_by_telegram_id
            await _publish_announcement_job(app, announcement_id, admin_id)


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
        last_text = f"最近一次完成的策略 1 运行：{last.started_at:%Y-%m-%d %H:%M} UTC，行情日期 {last.trading_date}。" if last else "还没有完成的策略 1 运行。"
    await update.message.reply_text("OK：Telegram 与数据库可用。" + last_text + ("策略每月自动运行已开启。" if context.application.bot_data.get("strategy_autorun") else "策略自动运行未开启（用 /runstrategy 手动运行）。"))


async def run_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with transaction(context.application.bot_data["session_factory"]) as session:
        context.application.bot_data["accounts"].require_maintainer(session, update.effective_user.id)
    await update.message.reply_text("开始运行策略 1（首次可能需要十几分钟）。完成后会通知你，"
                                    "并向已启用策略 1、权重有效、Premium 有效且开启信号通知的用户发送建议。")
    context.application.create_task(_run_strategy_job(context.application, update.effective_chat.id), update=update)


async def _run_strategy_job(application, chat_id: int) -> None:
    from . import strategy_service as strategy

    try:
        result = await strategy.run_and_notify(application)
        generation = result.get("generation", {})
        text = (f"策略 1 运行状态：{result['status']}，最后价格日 {result['as_of_bar'] or '无'}，已打分 {result['scored']} 只" + ("（沿用同一天已完成的运行）" if result["reused"] else "") +
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


_STAGED_REQUIRED = {
    "whitelist_add": "请输入：+(国家区号) (手机号) [备注]",
    "whitelist_show": "请输入：+(国家区号) (手机号)",
    "whitelist_edit": "请输入：+(国家区号) (手机号) 新备注；输入 - 可清空备注",
    "whitelist_remove": "请输入：+(国家区号) (手机号)",
    "admin_show": "请输入：(用户ID) 或 +(国家区号) (手机号)",
    "admin_add": "请输入：(用户ID) [备注]，或 +(国家区号) (手机号) [备注]",
    "admin_edit": "请输入：(用户ID) 新备注，或 +(国家区号) (手机号) 新备注；输入 - 可清空备注",
    "admin_remove": "请输入：(用户ID) 或 +(国家区号) (手机号)",
}
_STAGED_OPTIONAL = {
    "strategy_weights": "回复所有已启用策略的权重，例如 1=100；多策略示例 1=60 2=40（策略 2 尚未开放）",
    "whitelist_list": "回复页码可查看其他页",
    "admin_list": "回复页码可查看其他页",
    "portfolio": "回复页码可查看其他页",
    "settings": "回复 signals 或 announcements 可切换通知",
    "auto_notice_date": "回复 1–23 的交易日序号，或 random 设置每月随机提醒",
    "help": "回复帮助主题，例如 导入 或 问题",
}


def _validate_command_args(command: str, args: list[str]) -> None:
    if len(args) > 24:
        raise ValueError("参数过长。")
    if command == "strategy_weights":
        parse_weights(args)
    elif command.startswith("whitelist_") and command != "whitelist_list":
        minimum = 3 if command == "whitelist_edit" else 2
        if len(args) < minimum or (command in {"whitelist_show", "whitelist_remove"} and len(args) != 2):
            raise ValueError("手机号和备注格式不完整。")
        _whitelist_phone(args[:2])
        if command in {"whitelist_add", "whitelist_edit"}:
            _admin_label(args[2:], required=command == "whitelist_edit")
    elif command.startswith("admin_") and command not in {"admin_list", "admin_help"}:
        _, _, consumed = _admin_target(args)
        if command in {"admin_show", "admin_remove"} and len(args) != consumed:
            raise ValueError("此命令只接受一个用户 ID 或一个手机号。")
        if command in {"admin_add", "admin_edit"}:
            _admin_label(args[consumed:], required=command == "admin_edit")
    elif command in {"whitelist_list", "admin_list", "portfolio"}:
        if len(args) != 1 or not re.fullmatch(r"[1-9][0-9]{0,5}", args[0]):
            raise ValueError("页码必须是从 1 开始的整数。")
    elif command == "settings":
        if args not in (["signals"], ["announcements"]):
            raise ValueError("只能输入 signals 或 announcements。")
    elif command == "auto_notice_date":
        from .strategy_service import parse_notice_day
        if len(args) != 1:
            raise ValueError("只接受一个交易日序号或 random。")
        if args[0].lower() not in {"random", "随机"}:
            parse_notice_day(args[0])
    elif command == "help" and (len(args) != 1 or len(args[0]) > 32):
        raise ValueError("一次只能输入一个帮助主题。")


async def _staged_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    command = update.message.text.split(maxsplit=1)[0].split("@", 1)[0].lower().removeprefix("/")
    handler = _STAGED_HANDLERS[command]
    context.user_data.pop("pending_command_args", None)
    if context.user_data.pop("chat_allowed", None):
        context.user_data["chat_cancelled_by_command"] = True
    context.user_data.pop("chat_reply_id", None)
    context.user_data.pop("chat_reply_callback", None)
    if context.args:
        try:
            _validate_command_args(command, context.args)
        except ValueError as exc:
            context.user_data["pending_command_args"] = (command, datetime.now(UTC) + timedelta(minutes=5))
            await update.message.reply_text(f"{exc}\n{_STAGED_REQUIRED.get(command) or _STAGED_OPTIONAL[command]}。取消请发 /cancel。")
            return
        await handler(update, context)
        return
    if command in _STAGED_OPTIONAL:
        await handler(update, context)
    context.user_data["pending_command_args"] = (command, datetime.now(UTC) + timedelta(minutes=5))
    await update.message.reply_text(f"{_STAGED_REQUIRED.get(command) or _STAGED_OPTIONAL[command]}。请在下一条消息输入；取消请发 /cancel。")


async def _staged_command_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("pending_command_args")
    if not pending:
        return
    command, expiry = pending
    if datetime.now(UTC) >= expiry:
        context.user_data.pop("pending_command_args", None)
        await update.message.reply_text(f"输入已过期，请重新发送 /{command}。")
        return
    args = update.message.text.split()
    try:
        _validate_command_args(command, args)
    except ValueError as exc:
        await update.message.reply_text(f"{exc}\n{_STAGED_REQUIRED.get(command) or _STAGED_OPTIONAL[command]}。取消请发 /cancel。")
        return
    context.user_data.pop("pending_command_args", None)
    context.args = args
    await _STAGED_HANDLERS[command](update, context)


async def _staged_command_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("pending_command_args"):
        return
    await _staged_command_data(update, context)
    raise ApplicationHandlerStop


_STAGED_HANDLERS = {
    "strategy_weights": strategy_weights,
    "whitelist_add": whitelist_add, "whitelist_show": whitelist_show,
    "whitelist_edit": whitelist_edit, "whitelist_remove": whitelist_remove,
    "whitelist_list": whitelist_list, "admin_show": admin_show,
    "admin_add": admin_add, "admin_edit": admin_edit,
    "admin_remove": admin_remove, "admin_list": admin_list,
    "portfolio": portfolio, "settings": settings,
    "auto_notice_date": auto_notice_date, "help": help_command,
}


async def _interrupt_conversation_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Dispatch the new command AND end the previous PTB conversation state."""
    words = update.message.text.split()
    command = words[0].split("@", 1)[0].lower().removeprefix("/")
    context.args = words[1:]
    handler = context.application.bot_data["command_handlers"].get(command)
    if handler is not None:
        await handler(update, context)
    return ConversationHandler.END


def _conversation_callback(callback, *, entry: bool = False):
    @wraps(callback)
    async def guarded(update, context):
        if entry:
            context.user_data.pop("conversation_interrupted", None)
        elif context.user_data.get("conversation_interrupted"):
            if update.callback_query:
                await update.callback_query.answer("之前的操作已结束，请重新发送对应命令。", show_alert=True)
            else:
                await update.message.reply_text("之前的操作已结束，请重新发送对应命令。")
            return ConversationHandler.END
        try:
            return await callback(update, context)
        except Exception:
            # A failed new entry must not leave a previous state writable.
            context.user_data["conversation_interrupted"] = True
            raise
    return guarded


def configure_application(application: Application, accounts: AccountService, factory) -> Application:
    application.bot_data.update(accounts=accounts, session_factory=factory, enforce_premium=True)
    application.add_handler(TypeHandler(Update, authorization_gate), group=-2)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _staged_command_dispatch), group=-1)
    cancel_handler = CommandHandler("cancel", cancel)
    command_handlers = {command: _staged_command_entry for command in _STAGED_HANDLERS}
    application.bot_data["command_handlers"] = command_handlers
    # One conversation per user: starting another input flow replaces the old
    # state, and /cancel ends the entire active flow rather than just one of many.
    text_input = filters.TEXT & ~filters.COMMAND
    application.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("start", _conversation_callback(start, entry=True)),
            CommandHandler("add", _conversation_callback(add_start, entry=True)),
            CommandHandler("edit", _conversation_callback(edit_start, entry=True)),
            CommandHandler("remove", _conversation_callback(remove_start, entry=True)),
            CommandHandler("cash", _conversation_callback(cash_start, entry=True)),
            CommandHandler(["announce", "important_announce"], _conversation_callback(announce_start, entry=True)),
            CommandHandler("chat", _conversation_callback(chat_start, entry=True)),
            CallbackQueryHandler(_conversation_callback(chat_reply_start, entry=True), pattern=r"^chat-reply:"),
        ],
        states={
            CONTACT: [MessageHandler(filters.CONTACT, _conversation_callback(contact)),
                      MessageHandler(filters.ALL & ~filters.COMMAND, _conversation_callback(contact_reminder))],
            ADD_DATA: [MessageHandler(text_input, _conversation_callback(add_data))],
            EDIT_DATA: [MessageHandler(text_input, _conversation_callback(edit_data))],
            EDIT_CONFIRM: [CallbackQueryHandler(_conversation_callback(edit_confirm), pattern=r"^edit:")],
            REMOVE_DATA: [MessageHandler(text_input, _conversation_callback(remove_data))],
            REMOVE_CONFIRM: [CallbackQueryHandler(_conversation_callback(remove_confirm), pattern=r"^remove:")],
            CASH_DATA: [MessageHandler(text_input, _conversation_callback(cash_data))],
            ANNOUNCE_TEXT: [MessageHandler(text_input, _conversation_callback(announce_text))],
            ANNOUNCE_CONFIRM: [CallbackQueryHandler(_conversation_callback(announce_confirm), pattern=r"^announce:")],
            CHAT_TEXT: [MessageHandler(text_input, _conversation_callback(chat_text))],
            CHAT_REPLY_TEXT: [MessageHandler(text_input, _conversation_callback(chat_reply_text))],
        }, fallbacks=[cancel_handler, MessageHandler(filters.COMMAND, _interrupt_conversation_command)], allow_reentry=True,
    ))
    application.add_handler(CommandHandler(list(_STAGED_HANDLERS), _staged_command_entry))
    # A contact sent from an older /start keyboard must still work after a restart
    # or a lost conversation state. Once bound, contact() rejects stale prompts.
    application.add_handler(MessageHandler(filters.CONTACT, contact))
    application.add_handler(CommandHandler("cancel", cancel))
    for command, handler in [("help", help_command), ("privacy", privacy), ("subscribe", subscribe),
                             ("admin_help", admin_help), ("admin_list", admin_list), ("admin_show", admin_show),
                             ("admin_add", admin_add), ("admin_edit", admin_edit), ("admin_remove", admin_remove),
                             ("whitelist_add", whitelist_add), ("whitelist_list", whitelist_list),
                             ("whitelist_show", whitelist_show), ("whitelist_edit", whitelist_edit),
                             ("whitelist_remove", whitelist_remove),
                             ("strategy", strategy_command), ("strategy1", strategy_one),
                             ("strategy1_off", strategy_one_off), ("strategy2", strategy_two),
                             ("portfolio", portfolio), ("import", import_start), ("export", export_command), ("history", history), ("check", check), ("signals", signals), ("cooldown", cooldown), ("unbind", unbind_start), ("help_import", help_import), ("help_faq", help_faq), ("settings", settings), ("users", users_command), ("stats", stats_command), ("health", health), ("runstrategy", run_strategy)]:
        if command not in _STAGED_HANDLERS:
            command_handlers[command] = handler
            application.add_handler(CommandHandler(command, handler))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    application.add_handler(CallbackQueryHandler(unbind_confirm, pattern=r"^unbind:"))
    application.add_handler(CallbackQueryHandler(portfolio_page, pattern=r"^portfolio:"))
    application.add_handler(CallbackQueryHandler(sale_cooldown_confirm, pattern=r"^sale-cooldown:\d+:(yes|no)$"))
    application.add_handler(MessageHandler(filters.Document.FileExtension("csv"), csv_document))
    application.add_error_handler(_error)
    if application.job_queue:
        from .chat import maintain_chat_messages
        from .strategy_service import monthly_tick, retry_pending_notifications

        application.job_queue.run_repeating(monthly_tick, interval=3600, first=120, name="monthly-strategy")
        application.job_queue.run_repeating(retry_pending_notifications, interval=3600, first=60,
                                            name="strategy-notification-retry")
        application.job_queue.run_repeating(maintain_chat_messages, interval=3600, first=90,
                                            name="chat-message-maintenance")
        application.job_queue.run_repeating(resume_announcements, interval=60, first=5,
                                            name="announcement-recovery")
    return application

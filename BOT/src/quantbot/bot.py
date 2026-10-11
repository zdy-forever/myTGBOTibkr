from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from telegram import (
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

from .announcements import publish_announcement
from .chat import (
    create_message,
    deliver_chat_messages,
    purge_expired_chat_messages,
    reply_callback_data,
)
from .csv_io import export_portfolio_csv, import_portfolio_csv
from .db import transaction
from .display import price, qty, render_portfolio_pages
from .help_text import ADMIN_HELP, ADMIN_QUICK, QUICK, QUICK_PENDING, help_for
from .models import (
    AdminGrant,
    AdminPhoneGrant,
    Announcement,
    ChatMessage,
    NotificationPreference,
    PremiumSubscription,
    SubscriptionPayment,
    Transaction,
    User,
)
from .services import AccountService, PortfolioService, parse_money_text, system_stats
from .strategy_text import render_cooldowns

log = logging.getLogger(__name__)
HISTORY_TEXT = {"position_create": "新增持仓", "position_update": "修改持仓", "position_delete": "删除持仓", "cash_set": "设置现金", "cash_adjust": "调整现金",
                "plan_contribution": "定投入账", "csv_import": "导入 CSV"}
CONTACT, ADD_DATA, EDIT_DATA, REMOVE_CONFIRM, CASH_DATA, ANNOUNCE_TEXT, ANNOUNCE_CONFIRM, CHAT_TEXT, CHAT_REPLY_TEXT = range(9)

def _services(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["accounts"], PortfolioService()


SUBSCRIPTION_STARS = 400
SUBSCRIPTION_SECONDS = 30 * 24 * 60 * 60
SUBSCRIPTION_OFFER = "需要开通订阅：400 Telegram Stars / 30 天（约 US$7.99，实际价格因购买渠道而异）。发送 /subscribe 开通。"
ADMIN_COMMANDS = {"/admin_help", "/admin_list", "/admin_show", "/admin_add", "/admin_edit", "/admin_remove",
                  "/whitelist_add", "/whitelist_remove", "/announce", "/users", "/stats", "/health", "/runstrategy"}


def _access_text(status: str) -> str:
    if status == "whitelist":
        return "白名单 Premium 已激活（管理员白名单免费授权）。"
    if status == "paid":
        return "付费 Premium 已激活。"
    if status == "admin":
        return "管理员 Premium 已激活。"
    return SUBSCRIPTION_OFFER


async def authorization_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """First handler group: no command, callback, document or payment bypasses access control."""
    if update.effective_user is None:
        raise ApplicationHandlerStop
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        status = accounts.access_status(session, update.effective_user.id)
    message = update.effective_message
    command = (message.text or "").split(maxsplit=1)[0].split("@", 1)[0].lower() if message and message.text else ""
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
    if status in {"admin", "whitelist", "paid"}:
        return
    allowed = {"/start", "/help", "/help_import", "/help_faq", "/privacy", "/cancel"}
    if status != "unregistered":
        allowed.update({"/subscribe", "/unbind", "/chat"})
        if message and message.successful_payment:
            return
        if update.callback_query and (update.callback_query.data or "").startswith("unbind:"):
            return
        if message and message.text and not command.startswith("/") and getattr(context, "user_data", {}).get("chat_allowed"):
            return
    elif message and message.contact:
        return
    if command in allowed:
        return
    if update.callback_query:
        await update.callback_query.answer("请先验证手机号。" if status == "unregistered" else "请先开通 Premium。", show_alert=True)
    elif message:
        await message.reply_text("请先使用 /start 验证本人的 Telegram 手机号。" if status == "unregistered" else SUBSCRIPTION_OFFER)
    raise ApplicationHandlerStop


async def _error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("telegram update failed", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("操作失败，数据未保存。请检查输入后重试。")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if getattr(context, "user_data", None) is not None:
        context.user_data.pop("chat_allowed", None)
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        status = accounts.access_status(session, update.effective_user.id)
    if status != "unregistered":
        await update.message.reply_text("👋 欢迎回来。\n\n" + _access_text(status) +
                                        ("\n\n" + (ADMIN_QUICK if status == "admin" else QUICK)
                                         if status != "subscription_required" else ""),
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
            accounts.register(session, update.effective_user.id, item.phone_number)
            status = accounts.access_status(session, update.effective_user.id)
    except ValueError as exc:
        text = "这个手机号已经绑定了另一个 Telegram 账号。为防止被冒用，不能自动换绑，请联系管理员核实后处理。" if "already bound" in str(exc) else str(exc)
        await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    await update.message.reply_text(
        "✅ <b>注册完成</b>。\n" + _access_text(status) +
        ("\n\n" + (ADMIN_QUICK if status == "admin" else QUICK)
         if status != "subscription_required" else ""),
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
        await update.message.reply_text(_access_text(status))
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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⭐ 开通 Premium", url=link)]]),
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
        await update.message.reply_text("✅ 付费 Premium 已激活。\n\n" + QUICK, parse_mode=ParseMode.HTML)


def _whitelist_phone(args: list[str]) -> str:
    phone = " ".join(args)
    if not re.fullmatch(r"\+\d{1,3} [0-9 ]{6,20}", phone):
        raise ValueError("格式：+(国家区号) (手机号)，例如 /whitelist_add +86 13800138000")
    return phone


async def whitelist_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        phone = _whitelist_phone(context.args)
        with transaction(context.application.bot_data["session_factory"]) as session:
            created = context.application.bot_data["accounts"].whitelist_add(session, update.effective_user.id, phone)
    except (ValueError, PermissionError) as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_text("白名单已添加；已验证该号码的用户立即获得免费 Premium。" if created else "该号码已在白名单中。")


async def whitelist_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        phone = _whitelist_phone(context.args)
        with transaction(context.application.bot_data["session_factory"]) as session:
            removed = context.application.bot_data["accounts"].whitelist_remove(session, update.effective_user.id, phone)
    except (ValueError, PermissionError) as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_text("已从白名单移除；用户其他已付款权益仍然有效。" if removed else "该号码不在白名单中。")


def _admin_target(args: list[str]) -> tuple[str, int | str, int]:
    if not args:
        raise ValueError("请提供数字 Telegram ID 或 +国家区号 手机号；用法见 /admin_help。")
    if args[0].startswith("+"):
        if len(args) < 2:
            raise ValueError("手机号格式：+国家区号 手机号，例如 +86 13800138000。")
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
            verified = (session.scalar(select(User.id).where(
                User.phone_fingerprint == accounts.protector.fingerprint(target), User.is_active)) is not None
                if kind == "phone" else accounts.is_registered(session, target))
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    except PermissionError as exc:
        await update.message.reply_text(str(exc) if "Owner" in str(exc) else "仅已验证的管理员可添加管理员。")
        return
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
            removed = (accounts.admin_remove_phone(session, update.effective_user.id, target)
                       if kind == "phone" else accounts.admin_remove(session, update.effective_user.id, target))
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    except PermissionError as exc:
        await update.message.reply_text(str(exc) if "Owner" in str(exc) else "仅已验证的管理员可移除管理员。")
        return
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
    await query.edit_message_text("已解绑，你的全部数据已删除。需要时发 /start 重新绑定。" if done else "你还没有绑定。发 /start 开始绑定。")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    accounts, _ = _services(context)
    with transaction(context.application.bot_data["session_factory"]) as session:
        status = accounts.access_status(session, update.effective_user.id)
    text = (QUICK_PENDING if status == "subscription_required" else
            ADMIN_QUICK if status == "admin" and not context.args else
            help_for(context.args[0] if context.args else None, status != "unregistered"))
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def _require(session, update, context):
    accounts = context.application.bot_data["accounts"]
    user = accounts.require(session, update.effective_user.id)
    if context.application.bot_data.get("enforce_premium") and not accounts.has_premium_access(session, update.effective_user.id):
        raise PermissionError("Premium access required")
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
                                           exchange=exchange, actor=update.effective_user.id, record_sale=True)
        prompt, markup = _sale_cooldown_prompt(session, user.id)
    await query.edit_message_text(f"已更新 {symbol.upper()}。" + prompt, reply_markup=markup)
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
    return ("\n\n要把卖出的股票加入冷却期吗？（默认设置：加入）\n"
            "已默认加入；如果只是修改记录、没有实际卖出，请选择「不加入」。",
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
            fresh = row is not None and strategy.is_fresh(row) and strategy.covers_holdings(session, user.id, row)
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
                if application.bot_data.get("enforce_premium"):
                    with transaction(factory) as session:
                        if not application.bot_data["accounts"].has_premium_access(session, telegram_id):
                            continue
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
                f"自动通知日期：每月第 {prefs.auto_notice_day} 个美股交易日\n\n"
                "切换：/settings signals 或 /settings announcements\n修改日期：/auto_notice_date 5")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def chat_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
            raise ValueError("只接受一个整数，例如 /auto_notice_date 5。设置未修改。")
        day = strategy.parse_notice_day(context.args[0]) if context.args else None
        with transaction(app.bot_data["session_factory"]) as session:
            user = _require(session, update, context)
            prefs = session.get(NotificationPreference, user.id)
            if day is not None:
                prefs.auto_notice_day = day
            day, completed, enabled = prefs.auto_notice_day, prefs.last_notice_month, prefs.signals_enabled
    except PermissionError:
        await update.message.reply_text("请先使用 /start 注册。")
        return
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    tz, hour = app.bot_data.get("strategy_push", strategy.DEFAULT_PUSH)
    today = datetime.now(ZoneInfo(tz)).date()
    text = (f"{'已设置' if context.args else '当前设置'}：每月第 {day} 个美股交易日通知，只影响你自己。\n"
            f"通知时间：{tz} {hour:02d}:00 后的定时检查。\n"
            "用法：/auto_notice_date 5；不带数字可查看当前设置。\n"
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
    application.bot_data.update(accounts=accounts, session_factory=factory, enforce_premium=True)
    application.add_handler(TypeHandler(Update, authorization_gate), group=-1)
    cancel_handler = CommandHandler("cancel", cancel)
    application.add_handler(ConversationHandler([CommandHandler("start", start)], {CONTACT: [MessageHandler(filters.CONTACT, contact), MessageHandler(filters.ALL & ~filters.COMMAND, contact_reminder)]}, fallbacks=[cancel_handler], allow_reentry=True))
    # A contact sent from an older /start keyboard must still work after a restart
    # or a lost conversation state. Once bound, contact() rejects stale prompts.
    application.add_handler(MessageHandler(filters.CONTACT, contact))
    application.add_handler(ConversationHandler([CommandHandler("add", add_start)], {ADD_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_data)]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("edit", edit_start)], {EDIT_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_data)], REMOVE_CONFIRM: [CallbackQueryHandler(edit_confirm, pattern=r"^edit:")]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("remove", remove_start)], {REMOVE_CONFIRM: [CallbackQueryHandler(remove_confirm, pattern=r"^remove:")]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("cash", cash_start)], {CASH_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, cash_data)]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("announce", announce_start)], {ANNOUNCE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, announce_text)], ANNOUNCE_CONFIRM: [CallbackQueryHandler(announce_confirm, pattern=r"^announce:")]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CommandHandler("chat", chat_start)], {CHAT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, chat_text)]}, fallbacks=[cancel_handler]))
    application.add_handler(ConversationHandler([CallbackQueryHandler(chat_reply_start, pattern=r"^chat-reply:")], {CHAT_REPLY_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, chat_reply_text)]}, fallbacks=[cancel_handler]))
    for command, handler in [("help", help_command), ("privacy", privacy), ("subscribe", subscribe),
                             ("admin_help", admin_help), ("admin_list", admin_list), ("admin_show", admin_show),
                             ("admin_add", admin_add), ("admin_edit", admin_edit), ("admin_remove", admin_remove),
                             ("whitelist_add", whitelist_add), ("whitelist_remove", whitelist_remove),
                             ("portfolio", portfolio), ("import", import_start), ("export", export_command), ("history", history), ("check", check), ("signals", signals), ("cooldown", cooldown), ("unbind", unbind_start), ("help_import", help_import), ("help_faq", help_faq), ("settings", settings), ("users", users_command), ("stats", stats_command), ("health", health), ("runstrategy", run_strategy)]:
        application.add_handler(CommandHandler(command, handler))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    application.add_handler(CallbackQueryHandler(unbind_confirm, pattern=r"^unbind:"))
    application.add_handler(CallbackQueryHandler(portfolio_page, pattern=r"^portfolio:"))
    application.add_handler(CommandHandler("auto_notice_date", auto_notice_date))
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
    return application

"""Guided commands for strategy budgets, private pools and G2 reports."""
import asyncio
import re
from datetime import UTC, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop

from .allocations import (
    allocation_fraction,
    cash_budgets,
    invalidate_suggestions,
    set_strategy_cash,
)
from .db import transaction
from .models import StrategyPool, User, UserStrategy
from .services import normalize_symbol, parse_money_text


def parse_cash(text):
    text = re.sub(r'^/?cash(?:@\w+)?\s*', '', text.strip(), flags=re.IGNORECASE)
    match = re.fullmatch(r'([0-9]{1,4})\s+(.+)', text)
    if match:
        try:
            amount, currency = parse_money_text(match[2])
        except ValueError:
            pass  # Legacy "150 AUD" is an amount, not strategy 150.
        else:
            return int(match[1]), amount, currency
    # Legacy amount-only input is accepted only when no explicit budgets exist.
    try:
        amount, currency = parse_money_text(text)
    except ValueError as exc:
        raise ValueError('没看懂这个金额，请输入 1 200USD 或 0 1000USD。') from exc
    return None, amount, currency


async def save_cash_text(update, context, text):
    from .bot import _require
    number, amount, currency = parse_cash(text)
    factory = context.application.bot_data['session_factory']
    with transaction(factory) as session:
        _require(session, update, context, allow_unselected=True)
    from .fx import prepare_user_fx, refresh_market_quote
    try:
        if currency == 'AUD' and amount:
            await asyncio.to_thread(refresh_market_quote, factory)
        else:
            await prepare_user_fx(context, update.effective_user.id)
    except ValueError:
        # Public raw cash can still be recorded; strategy conversion fails closed.
        pass
    with transaction(context.application.bot_data['session_factory']) as session:
        user = _require(session, update, context, allow_unselected=True)
        if number is None:
            if cash_budgets(session, user.id):
                raise ValueError('当前为独立金额模式，请填写策略编号，例如 CASH 1 200USD；切换公共现金请用 CASH 0 金额USD。')
            number = 0
        set_strategy_cash(session, user, number, amount, currency, actor=update.effective_user.id)
        from .fx import conversion_note
        try:
            note = conversion_note(session, user.id, amount, currency)
        except ValueError as exc:
            note = str(exc)
    label = f'策略 {number} 独立现金' if number else '公共现金（已清除独立策略现金）'
    if currency not in {'USD', 'AUD'}:
        note = '策略只分析USD及按参考汇率折算的AUD；此币种现金不会用于建议。'
    note = '\n' + note if note else ''
    if currency == 'AUD':
        note = '\nAUD资金使用自动汇率估值，/fx查看或刷新。' + note
    await update.message.reply_text(f'{label}已替换为 {amount:,.2f} {currency}。\n' +
        ('比例模式可用，请用 /strategy_weights 核对比例。' if number == 0 else
         '独立金额模式已生效，比例设置暂停。其他策略的独立现金不变。') + note + '\n/strategy 查看下一步；/portfolio 核对账户。')


async def plain_cash(update, context):
    """CASH is an explicit command even while another input flow is pending."""
    context.user_data.clear()
    context.user_data['conversation_interrupted'] = True
    try:
        await save_cash_text(update, context, update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc) + '\n示例：CASH 1 200USD；CASH 0 1000USD。/cancel 取消。')
    raise ApplicationHandlerStop


async def fx_command(update, context):
    from .bot import _require
    from .fx import market_closed, refresh_market_quote
    try:
        with transaction(context.application.bot_data['session_factory']) as session:
            _require(session, update, context, allow_unselected=True)
        if context.args and context.args not in (['refresh'], ['刷新']):
            raise ValueError('汇率自动获取，无需手动填写。请直接发送 /fx 查看或刷新。')
        row = await asyncio.to_thread(refresh_market_quote, context.application.bot_data['session_factory'], force=True)
        source = {'truefx': 'TrueFX市场参考买价', 'yahoo_delayed': 'Yahoo延迟参考行情', 'twelvedata': 'Twelve Data实时参考行情'}[row.source]
        state = '\n外汇周末休市，当前为最近有效报价。' if market_closed(datetime.now(UTC)) else ''
        text = (f'自动汇率：1 AUD = {row.rate:g} USD\n来源：{source}\n'
                f'报价时间：{row.observed_at:%Y-%m-%d %H:%M:%S} UTC{state}\n'
                '录入AUD现金、查看建议时自动刷新，正常缓存30秒，无需手动填写。\n'
                '只影响资金估值，不修改原币记录，不会实际换汇或下单。\n'
                '可发 CASH 2 200AUD 或 CASH 0 1000AUD。')
    except PermissionError:
        text = '请先 /start 验证身份并确认Premium有效。'
    except ValueError as exc:
        text = str(exc)
    await update.message.reply_text(text)


def parse_pool_change(args):
    words = ' '.join(args).replace(',', ' ').replace('，', ' ').split()
    if words == ['reset']:
        return 'reset', []
    if words and words[0][0] in '+-' and len(words[0]) > 1:
        words = [words[0][0], words[0][1:]] + words[1:]
    aliases = {'+': 'add', 'add': 'add', '添加': 'add',
               '-': 'remove', 'remove': 'remove', '移除': 'remove'}
    action = aliases.get(words[0].lower()) if words else None
    if action is None or not 1 <= len(words[1:]) <= 60:
        raise ValueError('请用 + AAPL MSFT 添加，或 - AAPL MSFT 移除；每次可填写 1–60 个代码。')
    symbols = []
    prefix = '+' if action == 'add' else '-'
    for word in words[1:]:
        word = word.removeprefix(prefix)
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9.-]{0,23}', word):
            raise ValueError('股票代码格式不正确；添加和移除请分两条发送。')
        symbols.append(normalize_symbol(word))
    return action, list(dict.fromkeys(symbols))


def default_pool_symbols():
    from quant_strategy.pool import load_pool, sleeves
    pool = load_pool()
    if not pool or not pool.get('leaders'):
        raise ValueError('默认股票池尚未准备好，请先用 /check1 更新数据，再修改股票池。')
    return sorted(sleeves(pool))


async def pool_command(update, context):
    from .bot import _require
    try:
        action, symbols = parse_pool_change(context.args) if context.args else (None, [])
        with transaction(context.application.bot_data['session_factory']) as session:
            user = _require(session, update, context, strategy_id='strategy1')
            pool = session.get(StrategyPool, user.id)
            if action == 'reset':
                if pool is not None:
                    session.delete(pool)
                    invalidate_suggestions(session, user.id)
                status = '已恢复默认股票池。'
            else:
                current = list(pool.symbols) if pool is not None else default_pool_symbols()
                if action:
                    changed = ([s for s in symbols if s not in current] if action == 'add'
                               else [s for s in symbols if s in current])
                    updated = (current + changed if action == 'add'
                               else [s for s in current if s not in changed])
                    if len(updated) > max(200, len(current)):
                        raise ValueError('个人股票池最多 200 只，请先移除部分股票再添加。')
                    if changed:
                        if pool is not None:
                            pool.symbols = updated
                        else:
                            session.add(StrategyPool(user_id=user.id, symbols=updated))
                        invalidate_suggestions(session, user.id)
                    verb = '添加' if action == 'add' else '移除'
                    status = (f'已{verb}：' + '、'.join(changed) if changed else
                              '这些股票已在池中，无需重复添加。' if action == 'add' else
                              '这些股票不在池中，无需移除。')
                    skipped = [s for s in symbols if s not in changed]
                    if changed and skipped:
                        status += '\n已跳过：' + '、'.join(skipped)
                    status += f'\n当前股票池共 {len(updated)} 只。'
                    if not updated:
                        status += '股票池为空，暂停新增买入候选，已有持仓继续检查。'
                else:
                    status = f'你的 S1 股票池（共 {len(current)} 只）'
                    if pool:
                        status += '：'
                    else:
                        status += ' · 当前默认池：'
        pages = [status]
        if action is None:
            if current:
                for start in range(0, len(current), 80):
                    symbols_text = '、'.join(current[start:start + 80])
                    if start == 0:
                        pages[0] += '\n' + symbols_text
                    else:
                        pages.append(symbols_text)
            else:
                pages[0] += '\n股票池为空，请添加股票。'
        pages[-1] += ('\n\n/strategy1_pool + AAPL MSFT 添加股票\n'
            '/strategy1_pool - AAPL MSFT 移除股票\n'
            '/strategy1_pool 查看当前股票池；也可先发命令，再回复 + 或 - 和股票代码。\n'
            '只影响你自己的买入候选；已有持仓继续检查。无法取得完整行情或财务数据的股票不会参与买入建议。\n'
            '/check1 查看；/cancel 取消输入。')
        for page in pages:
            await update.message.reply_text(page)
    except (ValueError, PermissionError) as exc:
        await update.message.reply_text(str(exc))


async def strategy_two_off(update, context):
    from .bot import _refresh_user_menu, _require, strategy_command
    if context.args:
        await update.message.reply_text('/strategy2_off 不需要参数。')
        return
    with transaction(context.application.bot_data['session_factory']) as session:
        user = _require(session, update, context, allow_unselected=True)
        row = session.get(UserStrategy, (user.id, 'strategy2'))
        if row:
            session.delete(row)
    await _refresh_user_menu(context.application, update.effective_user.id)
    await update.message.reply_text('策略 2 已停用，保留现金、持仓和历史报告。' if row else '你尚未启用策略 2，无需停用。')
    await strategy_command(update, context)


async def check_two(update, context, *, show_progress=True):
    from . import strategy_service as s1
    from .bot import _require
    from .daily_service import eligible, s2_snapshot, s2_text
    app = context.application
    try:
        from .fx import prepare_user_fx
        await prepare_user_fx(context, update.effective_user.id)
        with transaction(app.bot_data['session_factory']) as session:
            user = _require(session, update, context, strategy_id='strategy2')
            allocation_fraction(session, user.id, 'strategy2')
            from .allocations import allocated_cash
            from .fx import to_usd
            raw_cash, raw_currency = allocated_cash(session, user.id, 'strategy2')
            to_usd(session, user.id, raw_cash, raw_currency)
            uid = user.id
        if show_progress:
            await update.message.reply_text('正在读取策略 2 的最近完整行情，首次预热可能需要几分钟。')
        lock = app.bot_data.setdefault('daily_strategy_lock', asyncio.Lock())
        async with lock:
            settings = s1.strategy_settings()
            market = s1.build_engine(settings).market
            now = datetime.now(UTC)
            today = now.astimezone(s1.NY).date()
            rows = await asyncio.to_thread(market.calendar, today, today + timedelta(days=10))
            from datetime import time
            for row in rows:
                day = datetime.fromisoformat(row['date']).date()
                closing = datetime.combine(day, time.fromisoformat(row['close']), tzinfo=s1.NY)
                # SIP requests use delayed data; immediately after the close,
                # today's bar may still be incomplete at the request cutoff.
                available_at = closing + timedelta(minutes=16 if market.feed == 'sip' else 0)
                if now < available_at:
                    today = day
                    break
            # Manual checks always identify the data date; exclude today's unclosed bar.
            data = await asyncio.to_thread(s2_snapshot, app.bot_data['session_factory'], market, today)
            with transaction(app.bot_data['session_factory']) as session:
                user = session.get(User, uid)
                if not eligible(session, app, user, 'strategy2', notify=False):
                    return
                body = s2_text(session, uid, data, today).replace('每日盘前报告', '手动检查（参考最近收盘数据）')
            await update.message.reply_text(body, parse_mode=ParseMode.HTML)
    except (ValueError, PermissionError, s1.StrategyUnavailable) as exc:
        await update.message.reply_text('策略 2 · ' + str(exc))
    except Exception:
        import logging
        logging.getLogger(__name__).exception('S2 manual check failed')
        await update.message.reply_text('策略 2 暂时无法取得完整行情，请稍后用 /check2 重试。')


def strategy_keyboard(enabled):
    rows = [[InlineKeyboardButton('查看策略 1' if 'strategy1' in enabled else '启用策略 1', callback_data='strategy:one'),
             InlineKeyboardButton('查看策略 2' if 'strategy2' in enabled else '启用策略 2', callback_data='strategy:two')],
            [InlineKeyboardButton('设置现金金额', callback_data='strategy:cash'),
             InlineKeyboardButton('设置资金比例', callback_data='strategy:weights')]]
    if 'strategy1' in enabled:
        rows.append([InlineKeyboardButton('修改 S1 股票池', callback_data='strategy:pool')])
    return InlineKeyboardMarkup(rows)


async def strategy_callback(update, context):
    from types import SimpleNamespace

    from . import bot
    query = update.callback_query
    await query.answer()
    action = query.data.partition(':')[2]
    # Commands use reply_text; preserve the caller's identity, not the bot message's author.
    proxy = SimpleNamespace(message=query.message, effective_user=update.effective_user,
                            effective_chat=update.effective_chat)
    context.user_data.clear()
    context.user_data['conversation_interrupted'] = True
    context.args = []
    if action in {'one', 'two'}:
        await (bot.strategy_one if action == 'one' else bot.strategy_two)(proxy, context)
    elif action == 'cash':
        await query.message.reply_text('选择资金方式并发送：\nCASH 1 200USD → 策略 1 独立现金\nCASH 2 300USD → 策略 2 独立现金\nCASH 0 500USD → 替换全部独立现金为公共现金，再用 /strategy_weights 设置比例。\n/cancel 取消。')
    elif action in {'weights', 'pool'}:
        command = 'strategy_weights' if action == 'weights' else 'strategy1_pool'
        await (bot.strategy_weights if action == 'weights' else pool_command)(proxy, context)
        context.user_data['pending_command_args'] = (command, datetime.now(UTC) + timedelta(minutes=5))

"""Durable per-session reports for both strategies, sharing the configured premarket time."""
import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import select
from telegram.constants import ParseMode
from telegram.error import TelegramError

from . import strategy_service as s1
from .allocations import allocated_cash, allocation_fraction
from .db import transaction
from .models import (
    DailyReport,
    NotificationPreference,
    Position,
    RunStatus,
    StrategyRun,
    StrategySnapshot,
    User,
    UserStrategy,
)
from .strategy_text import DISCLAIMER_HTML, signal_html

log = logging.getLogger(__name__)


def report_for(session, uid, strategy_id, today):
    return session.scalar(select(DailyReport).where(DailyReport.user_id == uid,
        DailyReport.strategy_id == strategy_id, DailyReport.session_date == today))


def needs_report(session, uid, strategy_id, today, now):
    report = report_for(session, uid, strategy_id, today)
    if strategy_id == 'strategy2' and report:
        from sgs.live import VERSION
        if report.strategy_version != VERSION:
            return True
    return report is None or (not report.data_ready and
                              now - s1._aware(report.created_at) >= timedelta(minutes=30))


def save_report(session, uid, strategy_id, today, body, *, ready=True, has_actions=None):
    version = None
    if strategy_id == 'strategy2':
        from sgs.live import VERSION
        version = VERSION
    report = report_for(session, uid, strategy_id, today)
    if report is None:
        session.add(DailyReport(user_id=uid, strategy_id=strategy_id, session_date=today,
                                body=body, data_ready=ready, strategy_version=version,
                                has_actions=has_actions))
    else:
        report.body = body
        report.has_actions = has_actions
        report.strategy_version = version
        report.created_at = datetime.now(UTC)
        report.data_ready = ready
        if ready:
            report.notified_at = None
            report.attempts = 0
            report.last_error = None


def email_users(session, strategy_id):
    """Users with a mailbox subscribed to this strategy get reports even with Telegram pushes off."""
    from .email_service import strategy_numbers
    from .models import EmailSubscription
    number = strategy_id.removeprefix('strategy')
    return {sub.user_id for sub in session.scalars(select(EmailSubscription))
            if number in strategy_numbers(sub.strategies)}


def save_failure(factory, app, ids, strategy_id, today):
    for uid in ids:
        with transaction(factory) as session:
            if not eligible(session, app, session.get(User, uid), strategy_id):
                continue
            number = strategy_id.removeprefix("strategy")
            body = (f"📊 <b>策略 {number} · 每日盘前报告 {today}</b>\n"
                    "状态：行情或账户数据暂不完整，今日暂不提供买卖建议。\n"
                    "系统会在盘前重试；请核对 /portfolio 与 /strategy 的现金配置。\n"
                    f"手动查看：{'/check1' if number == '1' else '/check2'}")
            save_report(session, uid, strategy_id, today, body, ready=False)


def eligible(session, app, user, strategy_id, *, notify=True):
    if not user or not user.is_active or not session.get(UserStrategy, (user.id, strategy_id)):
        return False
    prefs = session.get(NotificationPreference, user.id)
    if notify and (not prefs or not prefs.signals_enabled):
        return False
    accounts = app.bot_data.get('accounts')
    if app.bot_data.get('enforce_premium') and (not accounts or not accounts.has_premium_access(session, user.telegram_id)):
        return False
    try:
        allocation_fraction(session, user.id, strategy_id)
        from .fx import to_usd
        amount, currency = allocated_cash(session, user.id, strategy_id)
        if currency == 'AUD':
            to_usd(session, user.id, amount, currency)
    except ValueError:
        return False
    return True


def calendar_window(market, now, tz, hour):
    local = now.astimezone(ZoneInfo(tz))
    if local.hour < hour:
        return None
    today = local.date()
    rows = market.calendar(today.replace(day=1) - timedelta(days=10), today)
    days = sorted(date.fromisoformat(r['date']) for r in rows)
    row = next((r for r in rows if r['date'] == today.isoformat()), None)
    if row is None:
        return None
    opening = datetime.combine(today, time.fromisoformat(row['open']), tzinfo=s1.NY)
    if now >= opening:
        return None  # Never deliver an actionable premarket report after the open.
    previous = [d for d in days if d < today]
    if not previous:
        return None
    return today, previous[-1], [d for d in days if d.month == today.month], opening


def s2_snapshot(factory, market, today):
    from sgs.live import VERSION, snapshot
    with transaction(factory) as session:
        saved = session.get(StrategySnapshot, ('strategy2', today))
        if saved and saved.data.get('version') == VERSION:
            return saved.data
        previous = session.scalar(select(StrategySnapshot).where(
            StrategySnapshot.strategy_id == 'strategy2', StrategySnapshot.session_date < today)
            .order_by(StrategySnapshot.session_date.desc()).limit(1))
        prior_data = previous.data if previous and previous.data.get('version') == VERSION else None
    data = snapshot(market, today, prior_data)
    with transaction(factory) as session:
        saved = session.get(StrategySnapshot, ('strategy2', today))
        if saved:
            saved.data = data
        else:
            session.add(StrategySnapshot(strategy_id='strategy2', session_date=today, data=data))
    return data


def s2_text(session, user_id, data, today):
    return s2_report(session, user_id, data, today)[0]


def s2_report(session, user_id, data, today):
    """Report body plus whether it contains at least one executable order."""
    from sgs.live import MANAGED_TICKERS, PROFILE_LABEL, recommendations, weights_text
    fraction = allocation_fraction(session, user_id, 'strategy2')
    cash, currency = allocated_cash(session, user_id, 'strategy2')
    from .fx import conversion_note, to_usd
    fx_note = conversion_note(session, user_id, cash, currency)
    cash = to_usd(session, user_id, cash, currency)
    positions = list(session.scalars(select(Position).where(Position.user_id == user_id)))
    holdings = {}
    outside = []
    for p in positions:
        if p.symbol in MANAGED_TICKERS and p.currency == 'USD' and p.exchange in s1.US_EXCHANGES:
            holdings[p.symbol] = holdings.get(p.symbol, 0) + p.shares * fraction
        else:
            outside.append(p.symbol)
    actions = recommendations(data, holdings, cash)
    lines = [f'📊 <b>策略 2 · 每日盘前报告 {today}</b>', f'行情截至：{data["as_of"]}',
             f'策略配置：{PROFILE_LABEL}',
             f'当前趋势：<b>{escape(data["trend"])}</b>', f'可用现金：${cash:,.2f}',
             '目标配置：' + escape(weights_text(data['weights'])), '',
             '<b>今日操作</b>']
    if fx_note:
        lines.insert(6, escape(fx_note))
    lines += [f'{"买入" if a["action"] == "BUY" else "卖出"} {a["symbol"]} '
              f'{a["shares"]:.6f}'.rstrip('0').rstrip('.') +
              f' 股（约 ${a["shares"] * a["reference_price"]:.2f}，参考价 ${a["reference_price"]:.2f}）'
              for a in actions]
    if not actions:
        lines.append('今日暂无可执行的调整建议：持仓偏离未达到调仓条件、资金不足，或交易金额／预计手续费未达到成本要求。')
    if outside:
        lines.append('本策略只调整 ETF 范围内的持仓，其他证券保持不变。')
    lines += ['', '目标比例小幅变化不一定需要交易，请以「今日操作」为准。',
              '普通补仓和小幅调仓使用整股，单笔至少80美元，预计手续费不超过成交额0.5%；未满足时保留现金等待。',
              '清仓和必要风险减仓不受上述成本门槛限制，卖出可保留六位小数。先卖后买，数量不超过策略分配份额。',
              '手续费按IBKR Tiered估算，含每单0.05美元外部费用缓冲；实际费用以券商订单预览为准，换汇成本另计。参考上一交易日收盘价，实际成交价格会变化。',
              '成交后请更新持仓和现金；未更新时后续报告可能重复提醒。', DISCLAIMER_HTML]
    return '\n'.join(lines), bool(actions)


def s1_text(session, user, run, settings, today, days, app):
    return s1_report(session, user, run, settings, today, days, app)[0]


def s1_report(session, user, run, settings, today, days, app):
    """Report body plus whether it contains at least one actionable signal."""
    prefs = session.get(NotificationPreference, user.id)
    month = today.strftime('%Y-%m')
    due = prefs.last_notice_month != month and (
        prefs.auto_notice_day == days.index(today) + 1 if prefs.auto_notice_day else
        s1.random_notice_date(days, user.id, app.bot_data['accounts'].protector, prefs.random_notice_started_on) == today)
    data = s1.signals_for(session, user.id, run, settings)
    actions = [s for s in data['signals'] if due or s['kind'] != 'MONTHLY_BUY']
    lines = [f'📊 <b>策略 1 · 每日盘前报告 {today}</b>', f'行情截至：{escape(run.as_of_bar)}',
             '状态：已完成持仓与候选股票检查。', '<b>今日操作</b>']
    if data.get('cash_conversion'):
        lines.insert(3, escape(data['cash_conversion']))
    lines += [signal_html(s) for s in actions]
    if not actions:
        lines.append('今日无需操作。' if due else '今日不是你的月度买入提醒日；未发现需要调整的持仓。')
    if due:
        prefs.last_notice_month = month
    lines += ['', '每日检查；月度买入按 /auto_notice_date 设置的日期提醒。成交后请更新持仓和现金。', DISCLAIMER_HTML]
    return '\n'.join(lines), bool(actions)


async def daily_tick(context, now=None):
    app = context.application
    if not app.bot_data.get('strategy_autorun'):
        return
    lock = app.bot_data.setdefault('daily_strategy_lock', asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        try:
            await _daily_run(app, now or datetime.now(UTC))
        except Exception:
            log.exception('daily strategy preparation failed')


async def _daily_run(app, now):
    settings = s1.strategy_settings()
    market = s1.build_engine(settings).market
    tz, hour = app.bot_data.get('strategy_push', s1.DEFAULT_PUSH)
    window = await asyncio.to_thread(calendar_window, market, now, tz, hour)
    if window is None:
        return
    today, previous, days, opening = window
    factory = app.bot_data['session_factory']
    from .fx import refresh_market_quote
    from .models import CashAccount, StrategyCash
    with transaction(factory) as session:
        needs_fx = session.scalar(select(CashAccount.user_id).where(CashAccount.currency == 'AUD', CashAccount.balance > 0).limit(1)) or session.scalar(
            select(StrategyCash.user_id).where(StrategyCash.currency == 'AUD', StrategyCash.balance > 0).limit(1))
    if needs_fx:
        try:
            await asyncio.to_thread(refresh_market_quote, factory)
        except ValueError as exc:
            log.warning('AUD reference quote unavailable: %s', exc)
    for strategy_id in ('strategy1', 'strategy2'):
        with transaction(factory) as session:
            users = list(session.scalars(select(User).join(UserStrategy).where(UserStrategy.strategy_id == strategy_id)))
            mailed = email_users(session, strategy_id)
            ids = [u.id for u in users if eligible(session, app, u, strategy_id, notify=u.id not in mailed)
                   and needs_report(session, u.id, strategy_id, today, now)]
        if not ids:
            continue
        # Isolate strategy errors. One unavailable data feed does not block the other strategy.
        try:
            if strategy_id == 'strategy1':
                with transaction(factory) as session:
                    row = s1.latest_completed(session)
                    reuse = (row and row.trading_date == previous
                             and row.strategy_version == s1.load_library().strategy_info()["version"]
                             and s1.held_symbols(session) <= s1.analysis_symbols(s1.result_of(row)))
                    row_id = row.id if reuse else None
                if row_id is None:
                    result = await s1.refresh_run(app)
                    row_id = result['run_id']
                with transaction(factory) as session:
                    row = session.get(StrategyRun, row_id)
                    if row.status != RunStatus.COMPLETED or row.trading_date != previous:
                        raise ValueError('S1 尚无上一交易日完整数据')
                    run = s1.result_of(row)
            else:
                data = await asyncio.to_thread(s2_snapshot, factory, market, today)
            for uid in ids:
                try:
                    with transaction(factory) as session:
                        user = session.get(User, uid)
                        if not eligible(session, app, user, strategy_id,
                                        notify=uid not in email_users(session, strategy_id)):
                            continue
                        body, acts = (s1_report(session, user, run, settings, today, days, app)
                                      if strategy_id == 'strategy1' else s2_report(session, uid, data, today))
                        save_report(session, uid, strategy_id, today, body, has_actions=acts)
                except Exception:
                    log.exception('daily report generation failed for one user')
                    save_failure(factory, app, [uid], strategy_id, today)
        except Exception:
            log.exception('daily report generation failed: %s', strategy_id)
            save_failure(factory, app, ids, strategy_id, today)
    await deliver_reports(app, today, opening)
    from .email_service import deliver_emails
    try:
        await deliver_emails(app, today, opening)
    except Exception:
        log.exception('daily email delivery failed')


async def deliver_reports(app, today, opening):
    factory = app.bot_data['session_factory']
    with transaction(factory) as session:
        ids = list(session.scalars(select(DailyReport.id).where(DailyReport.session_date == today,
                    DailyReport.notified_at.is_(None), DailyReport.attempts < 5)))
    for report_id in ids:
        if datetime.now(UTC) >= opening:
            break
        with transaction(factory) as session:
            report = session.get(DailyReport, report_id)
            user = session.get(User, report.user_id) if report else None
            if not report or not eligible(session, app, user, report.strategy_id):
                continue
            if report.strategy_id == 'strategy2':
                from sgs.live import VERSION
                if report.strategy_version != VERSION:
                    report.last_error = '策略已升级；旧报告已作废，等待重新计算。'
                    report.attempts = 5
                    continue
            selection = session.get(UserStrategy, (user.id, report.strategy_id))
            if s1._aware(selection.enabled_at) > s1._aware(report.created_at):
                report.last_error = '设置已改变；旧报告已作废，请手动查看最新建议。'
                report.attempts = 5
                continue
            report.attempts += 1
            chat_id, body = user.telegram_id, report.body
        error = None
        try:
            await app.bot.send_message(chat_id, body, parse_mode=ParseMode.HTML)
        except TelegramError as exc:
            error = type(exc).__name__
        with transaction(factory) as session:
            report = session.get(DailyReport, report_id)
            if report:
                report.last_error = error
                if error is None:
                    report.notified_at = datetime.now(UTC)

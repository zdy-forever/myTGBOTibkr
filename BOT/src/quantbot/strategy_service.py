"""Glue between the bot's tables and the `quant_strategy` library.

One shared market run per month (prices, SEC fundamentals: no user data) is stored in `strategy_runs`. Each user is then scored one at a time from THAT user's own
positions and cash only. `signals` holds one shared reference row per stock (components, price; action HOLD = no shared action); `user_signals` holds what the user is told.
Nothing here places an order: users are notified and decide for themselves.
"""
from __future__ import annotations

import asyncio
import calendar as month_calendar
import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from html import escape as esc
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from .db import transaction
from .models import (
    CashAccount,
    NotificationPreference,
    Position,
    RunStatus,
    Signal,
    SignalAction,
    StrategyRun,
    Transaction,
    TransactionType,
    User,
    UserSignal,
)
from .strategy_text import DISCLAIMER_HTML, public_recommendation, render_check, signal_line

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
MAX_ATTEMPTS = 5
US_EXCHANGES = frozenset({"US", "NASDAQ", "NYSE", "AMEX", "ARCA", "NYSEARCA", "BATS"})
REFERENCE_NOTE = ("共享参考行：本次策略运行中该股票的 P/V/F 原始分（P 为原始值，越低越好）。"
                  "买卖建议按每位用户自己的持仓单独计算，见 user_signals。")


class StrategyUnavailable(RuntimeError):
    """The library is not installed or its Alpaca/SEC settings are missing."""


def load_library():
    try:
        import quant_strategy
    except ImportError as exc:                                       # pragma: no cover - depends on the deployment
        raise StrategyUnavailable("quant_strategy 未安装：先执行 pip install -e ../BACKEND") from exc
    return quant_strategy


def strategy_settings():
    qs = load_library()
    settings = qs.config.from_env()
    problems = settings.validate()
    if problems:
        raise StrategyUnavailable("策略配置不完整：" + "；".join(problems))
    return settings


def build_engine(settings):
    qs = load_library()
    market = qs.AlpacaReadOnly(settings.alpaca_key, settings.alpaca_secret, settings.alpaca_trading_url, settings.alpaca_data_url, settings.alpaca_feed)
    return qs.StrategyEngine(settings, market, qs.SecClient(settings.sec_user_agent, qs.config.CACHE_DIR))


# ---------------------------------------------------------------- reading the user's own data

def held_symbols(session: Session) -> set[str]:
    """Only ticker symbols (no owners, no quantities): the engine needs prices for names outside the ETF pool."""
    return set(session.scalars(
        select(Position.symbol).join(User).where(
            User.is_active, Position.currency == "USD", Position.exchange.in_(US_EXCHANGES))))


def portfolio_of(session: Session, user_id: str):
    qs = load_library()
    # The strategy and Alpaca identify US securities by ticker. Aggregate their
    # venue rows without losing quantity or cost; never merge foreign listings
    # or currencies into the same ticker (or value them at a US quote).
    totals = {}
    for p in session.scalars(select(Position).where(Position.user_id == user_id)):
        is_us = p.currency == "USD" and p.exchange in US_EXCHANGES
        key = p.symbol if is_us else f"{p.symbol}@{p.exchange}:{p.currency}"
        shares, cost = totals.get(key, (Decimal(0), Decimal(0)))
        totals[key] = (shares + p.shares, cost + p.shares * p.average_cost)
    holdings = {}
    for key, (shares, cost) in totals.items():
        if "@" in key:
            symbol, market = key.split("@", 1)
            exchange, currency = market.rsplit(":", 1)
        else:
            symbol, exchange, currency = key, "US", "USD"
        holdings[key] = qs.Holding(symbol, float(shares), float(cost / shares) if shares else 0.0,
                                   currency, exchange)
    cash = session.get(CashAccount, user_id)
    return qs.Portfolio(holdings=holdings, cash=float(cash.balance) if cash else 0.0, currency=cash.currency if cash else "USD")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def cooldowns(session: Session, user_id: str) -> tuple[dict, dict]:
    """Rebuild buy blocks from recorded sales, never from generated suggestions.

    Only explicitly marked edit/remove transactions count. Legacy edits may be
    corrections, so they must not retroactively become sales. The original
    transaction timestamp is retained when the user answers the prompt later.
    """
    from quant_strategy import logic

    rows = session.scalars(select(Transaction).where(
        Transaction.user_id == user_id,
        Transaction.type.in_([TransactionType.POSITION_UPDATE, TransactionType.POSITION_DELETE])))
    blocked = {}
    for row in rows:
        if not (row.after_data or {}).get("sale_cooldown", {}).get("enabled"):
            continue
        currency = (row.before_data or {}).get("currency", "USD")
        symbol = row.symbol if currency == "USD" and row.exchange in US_EXCHANGES else f"{row.symbol}@{row.exchange}:{currency}"
        until = _aware(row.created_at) + timedelta(days=logic.K1A.block_days)
        blocked[symbol] = max(blocked.get(symbol, until), until)
    return blocked, {}


def signals_for(session: Session, user_id: str, run, settings, now: datetime | None = None) -> dict:
    """Score ONE user from that user's own portfolio. The money to invest is the user's cash balance."""
    qs = load_library()
    portfolio = portfolio_of(session, user_id)
    deposit = portfolio.cash if portfolio.currency == "USD" else 0.0
    blocked, protected = cooldowns(session, user_id)
    data = qs.signals_for_user(run, portfolio, settings, deposit_usd=deposit, blocked=blocked, protected=protected, now=now).to_dict()
    return data


# ---------------------------------------------------------------- storing the shared run

def _trading_date(run) -> date:
    return date.fromisoformat(run.as_of_bar) if run.as_of_bar else datetime.now(UTC).date()


def store_run(session: Session, run, *, force: bool = False) -> tuple[StrategyRun, bool]:
    """-> (row, created). One row per (trading_date, strategy_version): a completed run is reused unless `force`; a failed or skipped one is replaced."""
    version = run.strategy["version"]
    row = session.scalar(select(StrategyRun).where(StrategyRun.trading_date == _trading_date(run), StrategyRun.strategy_version == version))
    if (row is not None and row.status == RunStatus.COMPLETED and run.status == "ok" and not force
            and analysis_symbols(run) <= analysis_symbols(result_of(row))):
        return row, False
    status = {"ok": RunStatus.COMPLETED, "stale": RunStatus.SKIPPED}.get(run.status, RunStatus.FAILED)
    fields = {"status": status, "started_at": datetime.fromisoformat(run.started_at), "completed_at": datetime.fromisoformat(run.finished_at),
              "error": None if status == RunStatus.COMPLETED else "；".join(run.notes[-3:])[:2000], "metadata_json": run.to_dict()}
    if row is not None and (row.metadata_json or {}).get("monthly_notified_at"):
        fields["metadata_json"]["monthly_notified_at"] = row.metadata_json["monthly_notified_at"]
    if row is None:
        row = StrategyRun(trading_date=_trading_date(run), strategy_version=version, **fields)
        session.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    session.flush()
    return row, True


def result_of(row: StrategyRun):
    result_class = load_library().RunResult
    return result_class.from_dict({key: value for key, value in row.metadata_json.items() if key in result_class.__dataclass_fields__})     # skip bookkeeping keys such as monthly_notified_at


def latest_completed(session: Session) -> StrategyRun | None:
    return session.scalar(select(StrategyRun).where(StrategyRun.status == RunStatus.COMPLETED).order_by(StrategyRun.started_at.desc()).limit(1))


# ---------------------------------------------------------------- storing what a user is told

def _reference_signal(session: Session, row: StrategyRun, run, symbol: str) -> Signal:
    sig = session.scalar(select(Signal).where(Signal.run_id == row.id, Signal.symbol == symbol, Signal.exchange == "US"))
    if sig is not None:
        return sig
    comps = run.symbols.get(symbol) or run.holding_symbols.get(symbol, {})
    price = run.prices.get(symbol) or comps.get("price")
    as_of = datetime.fromisoformat(run.as_of_bar).replace(tzinfo=UTC)
    d = lambda x: None if x is None else Decimal(str(round(x, 6)))
    sig = Signal(run_id=row.id, symbol=symbol, exchange="US", action=SignalAction.HOLD, reference_price=Decimal(str(price)), price_time=as_of,
                 p_score=d(comps.get("p_native")), f_score=d(comps.get("f_native")), v_score=d(comps.get("v_native")),
                 rationale=REFERENCE_NOTE, strategy_version=row.strategy_version)
    session.add(sig)
    session.flush()
    return sig


def store_user_signals(session: Session, row: StrategyRun, run, user_id: str, data: dict) -> int:
    """Insert the user's suggestions (idempotent per user and stock). Returns the number of new rows."""
    created = 0
    buys = [s for s in data["signals"] if s["kind"] == "MONTHLY_BUY"]
    for s in data["signals"]:
        ref = _reference_signal(session, row, run, s["symbol"])
        if session.scalar(select(UserSignal.id).where(UserSignal.user_id == user_id, UserSignal.signal_id == ref.id)):
            continue
        session.add(UserSignal(user_id=user_id, signal_id=ref.id, action=SignalAction(s["action"]), recommendation=signal_line(s, split=len(buys) == 2)))
        created += 1
    return created


def generate_for_all_users(factory, row_id: int, settings, *, user_ids=None, notice_month=None, notice_day=None,
                           accounts=None) -> dict:
    """One user at a time, each in its own transaction, so a problem with one portfolio never affects another."""
    with transaction(factory) as session:
        row = session.get(StrategyRun, row_id)
        run = result_of(row)
        query = select(User.id).join(NotificationPreference).where(User.is_active, NotificationPreference.signals_enabled)
        if user_ids is not None:
            query = query.where(User.id.in_(user_ids))
        user_ids = [uid for uid in session.scalars(query)
                    if accounts is None or accounts.has_premium_access(session, session.get(User, uid).telegram_id)]
    stats = {"users": len(user_ids), "with_signals": 0, "errors": 0}
    for user_id in user_ids:
        try:
            with transaction(factory) as session:
                prefs = session.get(NotificationPreference, user_id)
                user = session.get(User, user_id)
                if not user or not user.is_active or not prefs or not prefs.signals_enabled:
                    continue
                if accounts is not None and not accounts.has_premium_access(session, user.telegram_id):
                    continue
                if notice_day is not None and (prefs.auto_notice_day != notice_day or prefs.last_notice_month == notice_month):
                    continue
                data = signals_for(session, user_id, run, settings)
                if data["signals"] and store_user_signals(session, session.get(StrategyRun, row_id), run, user_id, data):
                    stats["with_signals"] += 1
                if notice_month is not None:
                    # Delivery has its own durable retries; do not regenerate
                    # this user's month just because Telegram is unavailable.
                    prefs.last_notice_month = notice_month
        except Exception:
            stats["errors"] += 1
            log.exception("strategy signals failed for one user")
    return stats


# ---------------------------------------------------------------- delivery

async def deliver_pending(application) -> tuple[int, int]:
    """Serialize manual/monthly delivery and background retries in this process."""
    lock = application.bot_data.setdefault("strategy_delivery_lock", asyncio.Lock())
    if lock.locked():
        return 0, 0
    async with lock:
        return await _deliver_pending(application)


async def retry_pending_notifications(context) -> None:
    """Resume persisted pending deliveries, independently of monthly autorun."""
    try:
        sent, failed = await deliver_pending(context.application)
        if sent or failed:
            log.info("strategy notification retry: sent=%s failed=%s", sent, failed)
    except Exception:
        log.exception("strategy notification retry failed")


async def _deliver_pending(application) -> tuple[int, int]:
    """Send each user ONE message with pending suggestions, up to MAX_ATTEMPTS."""
    factory = application.bot_data["session_factory"]
    with transaction(factory) as session:
        rows = session.execute(
            select(UserSignal, User.telegram_id).join(User, User.id == UserSignal.user_id).join(NotificationPreference, NotificationPreference.user_id == User.id)
            .where(UserSignal.notified_at.is_(None), UserSignal.notification_attempts < MAX_ATTEMPTS, User.is_active, NotificationPreference.signals_enabled)
            .order_by(UserSignal.user_id, UserSignal.action.desc(), UserSignal.id)).all()
        batches: dict[str, tuple[int, list[tuple[int, str]]]] = {}
        for us, telegram_id in rows:
            if application.bot_data.get("enforce_premium") and not application.bot_data["accounts"].has_premium_access(session, telegram_id):
                continue
            batches.setdefault(us.user_id, (telegram_id, []))[1].append((us.id, public_recommendation(us.recommendation)))
    sent = failed = 0
    for telegram_id, items in batches.values():
        text = ("📈 <b>本月策略建议</b>\n\n" + "\n\n".join(esc(t) for _, t in items) + "\n\n" + DISCLAIMER_HTML + "\n\n<i>用 /check 可按你当前的持仓重新查看。</i>")
        error = None
        try:
            while True:
                try:
                    await application.bot.send_message(telegram_id, text, parse_mode=ParseMode.HTML)
                    break
                except RetryAfter as exc:
                    await asyncio.sleep(float(exc.retry_after) + 0.2)
            sent += 1
        except TelegramError as exc:
            error = f"{type(exc).__name__}: {exc}"[:1000]
            failed += 1
        with transaction(factory) as session:
            for item_id, _ in items:
                current = session.get(UserSignal, item_id)
                if current is None:  # the user may have unbound while Telegram was awaited
                    continue
                current.notification_attempts += 1
                current.last_error = error
                if error is None:
                    current.notified_at = datetime.now(UTC)
        await asyncio.sleep(0.04)
    return sent, failed


# ---------------------------------------------------------------- freshness and the /check view

FRESH_HOURS = 12
RETRY_MINUTES = 30


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def is_fresh(row: StrategyRun, now: datetime | None = None) -> bool:
    return (row.strategy_version == load_library().strategy_info()["version"] and
            (_naive_utc(now or datetime.now(UTC)) - _naive_utc(row.started_at)) < timedelta(hours=FRESH_HOURS))


def analysis_symbols(run) -> set[str]:
    return set(run.symbols) | set(run.unscored) | set(run.holding_symbols) | set(run.holding_unscored)


def covers_holdings(session, user_id, row) -> bool:
    symbols = set(session.scalars(select(Position.symbol).where(
        Position.user_id == user_id, Position.currency == "USD", Position.exchange.in_(US_EXCHANGES))))
    return symbols <= analysis_symbols(result_of(row))


def recent_attempt(session: Session, now: datetime | None = None) -> StrategyRun | None:
    """A run of any status started in the last RETRY_MINUTES: /check does not start another one (protects the shared data feeds from repeated requests)."""
    since = _naive_utc(now or datetime.now(UTC)) - timedelta(minutes=RETRY_MINUTES)
    return session.scalar(select(StrategyRun).where(StrategyRun.started_at >= since).order_by(StrategyRun.started_at.desc()).limit(1))


def check_text(session: Session, user_id: str, row: StrategyRun, settings, now: datetime | None = None, top: int = 10) -> str:
    """What /check shows ONE user: the day's ranking (with that user's own D scores), the user's holdings, and the suggestions. Reads only that user's data."""
    from quant_strategy import config as qs_config
    from quant_strategy.signals import user_scores

    run = result_of(row)
    portfolio = portfolio_of(session, user_id)
    held_values = {s: h.shares * run.prices.get(s, h.avg_price) for s, h in portfolio.holdings.items() if h.currency == "USD"}
    scores = user_scores(run, held_values)
    data = signals_for(session, user_id, run, settings, now)
    return render_check(run, scores, held_values, data, qs_config.STRATEGY["min_buy_score"])


# ---------------------------------------------------------------- orchestration

async def refresh_run(application, *, force: bool = False) -> dict:
    """The shared run only (no notifications): used by /check and by the monthly push. Raises StrategyUnavailable if one is already running."""
    lock = application.bot_data.setdefault("strategy_lock", asyncio.Lock())
    if lock.locked():
        raise StrategyUnavailable("已有一次策略运行正在进行。")
    async with lock:
        settings = strategy_settings()
        factory = application.bot_data["session_factory"]
        with transaction(factory) as session:
            symbols = held_symbols(session)
        run = await asyncio.to_thread(build_engine(settings).run, symbols)
        with transaction(factory) as session:
            row, created = store_run(session, run, force=force)
            row_id, status = row.id, row.status
        return {"run_id": row_id, "status": status.value, "as_of_bar": run.as_of_bar, "scored": len(run.symbols), "notes": run.notes[-3:], "reused": not created, "settings": settings}


async def run_and_notify(application, *, force: bool = False, user_ids=None, notice_month=None, notice_day=None) -> dict:
    lock = application.bot_data.setdefault("strategy_notice_lock", asyncio.Lock())
    if lock.locked():
        raise StrategyUnavailable("已有一次策略推送正在进行。")
    async with lock:
        return await _run_and_notify(application, force=force, user_ids=user_ids,
                                     notice_month=notice_month, notice_day=notice_day)


async def _run_and_notify(application, *, force=False, user_ids=None, notice_month=None, notice_day=None) -> dict:
    """Monthly push: shared run (a completed run from the last 12 hours is reused), per-user scoring, notifications. Marks the month as done."""
    factory = application.bot_data["session_factory"]
    with transaction(factory) as session:
        row = latest_completed(session)
        reuse = None if force or row is None or not is_fresh(row) or not held_symbols(session) <= analysis_symbols(result_of(row)) else {"run_id": row.id, "status": "completed", "as_of_bar": row.metadata_json.get("as_of_bar", ""), "scored": len(row.metadata_json.get("symbols", {})),
                                                                       "notes": [], "reused": True}
    result = reuse or await refresh_run(application, force=force)
    settings = result.pop("settings", None) or strategy_settings()
    if result["status"] != RunStatus.COMPLETED.value:
        return result
    tz, _ = application.bot_data.get("strategy_push", DEFAULT_PUSH)
    notice_month = notice_month or datetime.now(ZoneInfo(tz)).strftime("%Y-%m")
    result["generation"] = await asyncio.to_thread(generate_for_all_users, factory, result["run_id"], settings,
                                                   user_ids=user_ids, notice_month=notice_month, notice_day=notice_day,
                                                   accounts=application.bot_data.get("accounts") if application.bot_data.get("enforce_premium") else None)
    result["sent"], result["failed"] = await deliver_pending(application)
    with transaction(factory) as session:
        mark_monthly_done(session, result["run_id"])
    return result


def mark_monthly_done(session: Session, row_id: int, now: datetime | None = None) -> None:
    row = session.get(StrategyRun, row_id)
    row.metadata_json = {**row.metadata_json, "monthly_notified_at": (now or datetime.now(UTC)).isoformat(timespec="seconds")}


DEFAULT_PUSH = ("Asia/Shanghai", 10)          # (time zone, local hour): 10:00 UTC+8 is the evening before the US open (21:30 / 22:30 UTC+8)
MAX_PUSH_ATTEMPTS_PER_DAY = 3


def due_for_monthly_run(session: Session, now: datetime | None = None, tz: str = DEFAULT_PUSH[0], hour: int = DEFAULT_PUSH[1]) -> bool:
    """Cheap, local part of the monthly-push test. The push belongs to ONE calendar day: the US first trading day of the month, seen on the local calendar (the push time is before
    the US open, so the local date equals the US session date). True when it is a local weekday at or after `hour`, the month's push has not been sent, and the day's earlier attempts
    (failed or skipped) are fewer than 3 and the last one is more than an hour old. The caller still has to confirm that today is the first trading day."""
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(tz))
    if local.weekday() >= 5 or local.hour < hour:
        return False
    day_start = _naive_utc(local.replace(hour=0, minute=0, second=0, microsecond=0))
    month_start = _naive_utc(local.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    failed = session.scalars(select(StrategyRun).where(StrategyRun.started_at >= day_start, StrategyRun.status != RunStatus.COMPLETED).order_by(StrategyRun.started_at.desc())).all()
    if len(failed) >= MAX_PUSH_ATTEMPTS_PER_DAY or (failed and _naive_utc(now or datetime.now(UTC)) - _naive_utc(failed[0].started_at) < timedelta(hours=1)):
        return False
    rows = session.scalars(select(StrategyRun).where(StrategyRun.started_at >= month_start, StrategyRun.status == RunStatus.COMPLETED)).all()
    return not any((r.metadata_json or {}).get("monthly_notified_at") for r in rows)


def parse_notice_day(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,2}", value):
        raise ValueError("请输入 1–23 的整数，例如 /auto_notice_date 5；不接受小数、负数或其他文字。")
    day = int(value)
    if not 1 <= day <= 23:
        raise ValueError(f"请输入 1–23 的整数。你输入的第 {day} 个交易日在本月、下个月及其他月份都不存在。"
                         "交易日不含周末及休市日，一个月最多只有 23 个工作日；2 月更少。设置未修改。")
    return day


def month_trading_days(settings, year: int, month: int, calendar=None) -> list[date]:
    """Use the actual market calendar, including holidays. Never guess on failure."""
    first = date(year, month, 1)
    last = date(year, month, month_calendar.monthrange(year, month)[1])
    days = _calendar_dates(settings, first, last, calendar)
    if not days:
        raise StrategyUnavailable("交易日历没有返回该月数据，暂时无法确认通知日期。")
    return days


def _calendar_dates(settings, first, last, calendar=None):
    try:
        if calendar is None:
            qs = load_library()
            calendar = qs.AlpacaReadOnly(settings.alpaca_key, settings.alpaca_secret, settings.alpaca_trading_url, settings.alpaca_data_url, settings.alpaca_feed).calendar
        days = sorted({date.fromisoformat(item["date"]) for item in calendar(first, last)})
        days = [day for day in days if first <= day <= last and day.weekday() < 5]
        if not days:
            raise ValueError("empty market calendar")
        return days
    except Exception as exc:
        raise StrategyUnavailable("暂时无法取得交易日历；不会用普通工作日代替交易日。") from exc


def notice_date_preview(settings, today: date, day: int, completed=None, calendar=None) -> str:
    """One calendar request covers this month and the next eleven months."""
    first = today.replace(day=1)
    months = []
    for offset in range(12):
        year, month0 = divmod(first.year * 12 + first.month - 1 + offset, 12)
        months.append(date(year, month0 + 1, 1))
    last_month = months[-1]
    last = last_month.replace(day=month_calendar.monthrange(last_month.year, last_month.month)[1])
    try:
        days = _calendar_dates(settings, first, last, calendar)
    except StrategyUnavailable:
        return ("本月：交易日历暂不可用，尚未确认。\n下个月：交易日历暂不可用，尚未确认。\n"
                "设置已保存，后续月份也尚未确认；调度会等取得真实交易日历后再判断。")
    grouped = {month: [d for d in days if (d.year, d.month) == (month.year, month.month)] for month in months}
    lines, missing, unknown = [], [], []
    for i, month in enumerate(months):
        sessions = grouped[month]
        label = f"{'本月' if i == 0 else '下个月'}（{month:%Y-%m}）"
        if not sessions:
            unknown.append(month)
            status = "日历数据缺失，尚未确认。"
        elif day > len(sessions):
            missing.append(month)
            status = f"不能生效，只有 {len(sessions)} 个交易日；本月跳过。"
        else:
            target = sessions[day - 1]
            status = f"可生效，对应 {target:%Y-%m-%d}。"
            if i == 0 and completed == month.strftime("%Y-%m"):
                status = f"对应 {target:%Y-%m-%d}，但本月已完成通知任务，不重复发送；新日期从下个月起适用。"
            elif i == 0 and target < today:
                status = f"对应 {target:%Y-%m-%d} 已过，本月不补发。"
        if i < 2:
            lines.append(f"{label}：{status}")
    lines.append(f"已检查范围：{first:%Y-%m} 至 {last_month:%Y-%m}。")
    if missing:
        lines.append(f"范围内首次确定无法生效的月份：{missing[0]:%Y-%m}。")
        lines.append("交易日不足、会跳过的月份：" + "、".join(f"{m:%Y-%m}（{len(grouped[m])} 个交易日）" for m in missing) + "。")
        lines.append("其余有完整日历数据的月份都有对应交易日；不足的月份跳过后，后续符合条件的月份会恢复通知。")
    elif not unknown:
        lines.append("未来 12 个月均有这个交易日，范围内没有因交易日不足而失效的月份。")
    else:
        lines.append("有日历数据的月份均有这个交易日，但尚不能确定第一次失效的月份。")
    if unknown:
        lines.append("日历缺失、尚未确认的月份：" + "、".join(f"{m:%Y-%m}" for m in unknown) + "。")
    lines.append(f"{last_month:%Y-%m} 之后尚未核实；实际执行以届时交易所日历为准。")
    return "\n".join(lines)


def first_trading_day(settings, year: int, month: int, calendar=None) -> date:
    return month_trading_days(settings, year, month, calendar)[0]


def due_notice_users(session, now: datetime, day: int, tz: str, accounts=None) -> list[str]:
    local = now.astimezone(ZoneInfo(tz))
    month = local.strftime("%Y-%m")
    result = []
    for prefs in session.scalars(select(NotificationPreference).join(User).where(
            User.is_active, NotificationPreference.signals_enabled, NotificationPreference.auto_notice_day == day)):
        if accounts is not None and not accounts.has_premium_access(session, session.get(User, prefs.user_id).telegram_id):
            continue
        if prefs.last_notice_month == month:
            continue
        if prefs.notice_attempt_date == local.date():
            if prefs.notice_attempts >= MAX_PUSH_ATTEMPTS_PER_DAY:
                continue
            if prefs.notice_last_attempt_at and _naive_utc(now) - _naive_utc(prefs.notice_last_attempt_at) < timedelta(hours=1):
                continue
        result.append(prefs.user_id)
    return result


async def monthly_tick(context, now: datetime | None = None) -> None:
    """Run once for today's subscribers; persist per-user completion and attempts."""
    app = context.application
    if not app.bot_data.get("strategy_autorun"):
        return
    tz, hour = app.bot_data.get("strategy_push", DEFAULT_PUSH)
    now = now or datetime.now(UTC)
    local = now.astimezone(ZoneInfo(tz))
    if local.weekday() >= 5 or local.hour < hour:
        return
    lock = app.bot_data.setdefault("monthly_tick_lock", asyncio.Lock())
    if lock.locked() or any(app.bot_data.get(key) and app.bot_data[key].locked()
                            for key in ("strategy_lock", "strategy_notice_lock")):
        return
    async with lock:
        await _monthly_tick(app, now, local, tz)


async def _monthly_tick(app, now, local, tz):
    try:
        settings = strategy_settings()
        today = local.date()
        cache = app.bot_data.get("notice_calendar")
        if cache is None or cache[0] != today:
            days = await asyncio.to_thread(month_trading_days, settings, today.year, today.month)
            app.bot_data["notice_calendar"] = (today, days)
        else:
            days = cache[1]
        if today not in days:
            return
        ordinal = days.index(today) + 1
        with transaction(app.bot_data["session_factory"]) as session:
            user_ids = due_notice_users(session, now, ordinal, tz,
                                        app.bot_data.get("accounts") if app.bot_data.get("enforce_premium") else None)
            for user_id in user_ids:
                prefs = session.get(NotificationPreference, user_id)
                prefs.notice_attempts = prefs.notice_attempts + 1 if prefs.notice_attempt_date == today else 1
                prefs.notice_attempt_date = today
                prefs.notice_last_attempt_at = now
        if user_ids:
            log.info("monthly strategy run: %s", await run_and_notify(
                app, user_ids=user_ids, notice_month=local.strftime("%Y-%m"), notice_day=ordinal))
    except StrategyUnavailable as exc:
        log.warning("monthly strategy run skipped: %s", exc)
    except Exception:
        log.exception("monthly strategy run failed")

"""Glue between the bot's tables and the `quant_strategy` library.

One shared market run per month (prices, SEC fundamentals: no user data) is stored in `strategy_runs`. Each user is then scored one at a time from THAT user's own
positions and cash only. `signals` holds one shared reference row per stock (components, price; action HOLD = no shared action); `user_signals` holds what the user is told.
Nothing here places an order: users are notified and decide for themselves.
"""
from __future__ import annotations

import asyncio
import logging
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
    User,
    UserSignal,
)
from .strategy_text import DISCLAIMER_HTML, KIND_TITLE, render_check, signal_line

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
MAX_ATTEMPTS = 5
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
        select(Position.symbol).join(User).where(User.is_active, Position.currency == "USD")))


def portfolio_of(session: Session, user_id: str):
    qs = load_library()
    holdings = {p.symbol: qs.Holding(p.symbol, float(p.shares), float(p.average_cost), p.currency, p.exchange)
                for p in session.scalars(select(Position).where(Position.user_id == user_id))}
    cash = session.get(CashAccount, user_id)
    return qs.Portfolio(holdings=holdings, cash=float(cash.balance) if cash else 0.0, currency=cash.currency if cash else "USD")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def cooldowns(session: Session, user_id: str) -> tuple[dict, dict]:
    """The strategy's anti-churn rules, rebuilt from this user's own earlier suggestions (no extra table):
    a name sold by a K1A / K5x suggestion is not bought back for 180 days; a name bought by one cannot be sold by K1A / K5x for 180 days (365 if it is a re-buy)."""
    from quant_strategy import logic

    rows = session.execute(
        select(UserSignal, Signal.symbol).join(Signal, Signal.id == UserSignal.signal_id).where(UserSignal.user_id == user_id)
        .order_by(UserSignal.created_at, UserSignal.id)).all()
    blocked, protected, sold = {}, {}, set()
    for us, symbol in rows:
        at = _aware(us.created_at)
        if us.action == SignalAction.SELL:
            blocked[symbol] = at + timedelta(days=logic.K1A.block_days)
            sold.add(symbol)
        elif us.recommendation.startswith(f"【{KIND_TITLE['K1A_REPLACE']}】"):
            protected[symbol] = at + timedelta(days=logic.K1A.block_days)
        elif us.recommendation.startswith(f"【{KIND_TITLE['K5X_TRIM']}】"):
            protected[symbol] = at + timedelta(days=logic.OA.rebuy_protect_days if symbol in sold else logic.K1A.block_days)
    return blocked, protected


def signals_for(session: Session, user_id: str, run, settings, now: datetime | None = None) -> dict:
    """Score ONE user from that user's own portfolio. The money to invest is the user's cash balance."""
    qs = load_library()
    portfolio = portfolio_of(session, user_id)
    deposit = portfolio.cash if portfolio.currency == "USD" else 0.0
    blocked, protected = cooldowns(session, user_id)
    data = qs.signals_for_user(run, portfolio, settings, deposit_usd=deposit, blocked=blocked, protected=protected, now=now).to_dict()
    from quant_strategy.signals import user_scores

    held_values = {s: h.shares * run.prices.get(s, h.avg_price) for s, h in portfolio.holdings.items() if h.currency == "USD"}
    scores = user_scores(run, held_values)
    rank = {s: i for i, s in enumerate(sorted(scores, key=lambda s: (-scores[s]["total"], s)), 1)}
    for signal in data["signals"]:
        signal["rank"] = rank.get(signal["symbol"])                       # shown instead of the score, which users never see
    return data


# ---------------------------------------------------------------- storing the shared run

def _trading_date(run) -> date:
    return date.fromisoformat(run.as_of_bar) if run.as_of_bar else datetime.now(UTC).date()


def store_run(session: Session, run, *, force: bool = False) -> tuple[StrategyRun, bool]:
    """-> (row, created). One row per (trading_date, strategy_version): a completed run is reused unless `force`; a failed or skipped one is replaced."""
    version = run.strategy["version"]
    row = session.scalar(select(StrategyRun).where(StrategyRun.trading_date == _trading_date(run), StrategyRun.strategy_version == version))
    if row is not None and row.status == RunStatus.COMPLETED and run.status == "ok" and not force:
        return row, False
    status = {"ok": RunStatus.COMPLETED, "stale": RunStatus.SKIPPED}.get(run.status, RunStatus.FAILED)
    fields = {"status": status, "started_at": datetime.fromisoformat(run.started_at), "completed_at": datetime.fromisoformat(run.finished_at),
              "error": None if status == RunStatus.COMPLETED else "；".join(run.notes[-3:])[:2000], "metadata_json": run.to_dict()}
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
    comps = run.symbols.get(symbol, {})
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


def generate_for_all_users(factory, row_id: int, settings) -> dict:
    """One user at a time, each in its own transaction, so a problem with one portfolio never affects another."""
    with transaction(factory) as session:
        row = session.get(StrategyRun, row_id)
        run = result_of(row)
        user_ids = list(session.scalars(select(User.id).join(NotificationPreference).where(User.is_active, NotificationPreference.signals_enabled)))
    stats = {"users": len(user_ids), "with_signals": 0, "errors": 0}
    for user_id in user_ids:
        try:
            with transaction(factory) as session:
                data = signals_for(session, user_id, run, settings)
                if data["signals"] and store_user_signals(session, session.get(StrategyRun, row_id), run, user_id, data):
                    stats["with_signals"] += 1
        except Exception:
            stats["errors"] += 1
            log.exception("strategy signals failed for one user")
    return stats


# ---------------------------------------------------------------- delivery

async def deliver_pending(application) -> tuple[int, int]:
    """Send each user ONE message with all of their pending suggestions. Records every attempt; safe to repeat after a restart."""
    factory = application.bot_data["session_factory"]
    with transaction(factory) as session:
        rows = session.execute(
            select(UserSignal, User.telegram_id).join(User, User.id == UserSignal.user_id).join(NotificationPreference, NotificationPreference.user_id == User.id)
            .where(UserSignal.notified_at.is_(None), UserSignal.notification_attempts < MAX_ATTEMPTS, User.is_active, NotificationPreference.signals_enabled)
            .order_by(UserSignal.user_id, UserSignal.action.desc(), UserSignal.id)).all()
        batches: dict[str, tuple[int, list[tuple[int, str]]]] = {}
        for us, telegram_id in rows:
            batches.setdefault(us.user_id, (telegram_id, []))[1].append((us.id, us.recommendation))
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
    return (_naive_utc(now or datetime.now(UTC)) - _naive_utc(row.started_at)) < timedelta(hours=FRESH_HOURS)


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


async def run_and_notify(application, *, force: bool = False) -> dict:
    """Monthly push: shared run (a completed run from the last 12 hours is reused), per-user scoring, notifications. Marks the month as done."""
    factory = application.bot_data["session_factory"]
    with transaction(factory) as session:
        row = latest_completed(session)
        reuse = None if force or row is None or not is_fresh(row) else {"run_id": row.id, "status": "completed", "as_of_bar": row.metadata_json.get("as_of_bar", ""), "scored": len(row.metadata_json.get("symbols", {})),
                                                                       "notes": [], "reused": True}
    result = reuse or await refresh_run(application, force=force)
    settings = result.pop("settings", None) or strategy_settings()
    if result["status"] != RunStatus.COMPLETED.value:
        return result
    result["generation"] = await asyncio.to_thread(generate_for_all_users, factory, result["run_id"], settings)
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


def first_trading_day(settings, year: int, month: int, calendar=None) -> date:
    """First US market session of the month from Alpaca's calendar; if it cannot be reached, the first weekday."""
    first = date(year, month, 1)
    try:
        if calendar is None:
            qs = load_library()
            calendar = qs.AlpacaReadOnly(settings.alpaca_key, settings.alpaca_secret, settings.alpaca_trading_url, settings.alpaca_data_url, settings.alpaca_feed).calendar
        days = sorted(item["date"] for item in calendar(first, first + timedelta(days=9)))
        return date.fromisoformat(days[0])
    except Exception:
        log.warning("market calendar unavailable, using the first weekday as the first trading day")
        return next(first + timedelta(days=i) for i in range(7) if (first + timedelta(days=i)).weekday() < 5)


async def monthly_tick(context, now: datetime | None = None) -> None:
    """Hourly job. Pushes only on the US first trading day of the month (see due_for_monthly_run), at/after the configured local hour."""
    app = context.application
    if not app.bot_data.get("strategy_autorun"):
        return
    tz, hour = app.bot_data.get("strategy_push", DEFAULT_PUSH)
    with transaction(app.bot_data["session_factory"]) as session:
        due = due_for_monthly_run(session, now, tz, hour)
    if not due:
        return
    try:
        settings = strategy_settings()
        today = (now or datetime.now(UTC)).astimezone(ZoneInfo(tz)).date()
        if await asyncio.to_thread(first_trading_day, settings, today.year, today.month) != today:
            return
        log.info("monthly strategy run: %s", await run_and_notify(app))
    except StrategyUnavailable as exc:
        log.warning("monthly strategy run skipped: %s", exc)
    except Exception:
        log.exception("monthly strategy run failed")

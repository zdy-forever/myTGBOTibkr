from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from telegram.error import Forbidden

pytest.importorskip("quant_strategy")

from quant_strategy import RunResult, config, strategy_info  # noqa: E402

from quantbot import strategy_service as svc  # noqa: E402
from quantbot.models import (  # noqa: E402
    CashAccount,
    NotificationPreference,
    Position,
    RunStatus,
    Signal,
    StrategyRun,
    User,
    UserSignal,
)
from quantbot.strategy_text import DISCLAIMER, render_user_signals  # noqa: E402

SETTINGS = config.Settings(min_sale_usd=330.0)
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)          # Monday 11:00 New York
NAMES = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]


def make_run(status="ok", as_of="2026-10-09", started=None) -> RunResult:
    started = (started or NOW).isoformat(timespec="seconds")
    symbols = {
        s: {"price": 100.0, "sleeves": ["IYW"], "p_native": 10.0 + 2 * i, "v_native": 20.0 - 2 * i, "f_native": 10.0, "q_native": 5.0,
            "beta": [1.0 + 0.1 * i, 0.1 * i, -0.1 * i]}
        for i, s in enumerate(NAMES)
    }
    return RunResult(run_id=f"run-{as_of}-{status}", started_at=started, finished_at=started,
                     status=status, as_of_bar=as_of if status != "failed" else "", strategy=strategy_info(), pool={"built": "2026-09-01"},
                     symbols=symbols if status != "failed" else {}, unscored={}, prices={s: 100.0 for s in NAMES} if status != "failed" else {},
                     notes=[] if status == "ok" else ["something went wrong"])


def add_user(factory, telegram_id, *, cash="1000", positions=(), signals_enabled=True):
    with factory.begin() as session:
        user = User(telegram_id=telegram_id, phone_fingerprint=f"fp{telegram_id}")
        session.add(user)
        session.flush()
        session.add(CashAccount(user_id=user.id, balance=Decimal(cash), currency="USD"))
        session.add(NotificationPreference(user_id=user.id, signals_enabled=signals_enabled))
        for symbol, shares in positions:
            session.add(Position(user_id=user.id, symbol=symbol, exchange="US", shares=Decimal(shares), average_cost=Decimal("100")))
        return user.id


def test_store_run_is_idempotent_and_failed_runs_are_replaced(factory):
    with factory.begin() as session:
        failed, created = svc.store_run(session, make_run("failed"))
        assert created and failed.status == RunStatus.FAILED and "something went wrong" in failed.error
    with factory.begin() as session:
        ok, created = svc.store_run(session, make_run("ok"))
        assert created and ok.status == RunStatus.COMPLETED and svc.result_of(ok).symbols.keys() == set(NAMES)
    with factory.begin() as session:
        again, created = svc.store_run(session, make_run("ok"))
        assert not created and again.id == ok.id
        assert len(session.scalars(select(StrategyRun)).all()) == 2          # failed one has no price date, so it is its own row


def test_stale_run_is_skipped_not_completed(factory):
    with factory.begin() as session:
        row, _ = svc.store_run(session, make_run("stale"))
        assert row.status == RunStatus.SKIPPED and svc.latest_completed(session) is None


def test_each_user_is_scored_from_their_own_portfolio_only(factory):
    a = add_user(factory, 1, cash="200")
    b = add_user(factory, 2, cash="200", positions=[("AAA", "5")])
    run = make_run()
    with factory.begin() as session:
        da, db = (svc.signals_for(session, uid, run, SETTINGS, NOW) for uid in (a, b))
    assert da["summary"]["equity"] == 200.0 and db["summary"]["equity"] == 700.0           # B's 5 x 100 holding never leaks into A
    assert da["summary"]["top_candidates"] != db["summary"]["top_candidates"] or da["signals"] != db["signals"]
    assert all(s["action"] == "BUY" for s in da["signals"]) and DISCLAIMER in render_user_signals(da)


def test_generate_creates_shared_reference_rows_and_per_user_rows(factory):
    a = add_user(factory, 1, cash="200")
    b = add_user(factory, 2, cash="200")
    add_user(factory, 3, cash="200", signals_enabled=False)
    with factory.begin() as session:
        row, _ = svc.store_run(session, make_run())
        row_id = row.id
    stats = svc.generate_for_all_users(factory, row_id, SETTINGS)
    assert stats == {"users": 2, "with_signals": 2, "errors": 0}
    with factory.begin() as session:
        refs = session.scalars(select(Signal)).all()
        rows = session.scalars(select(UserSignal)).all()
        assert {r.user_id for r in rows} == {a, b}                                          # the user with signals off got nothing
        assert len({(r.run_id, r.symbol) for r in refs}) == len(refs) and all(r.action.value == "HOLD" for r in refs)
        assert len(refs) < len(rows) or len(refs) == len({r.signal_id for r in rows})         # users share reference rows, never the recommendation
    assert svc.generate_for_all_users(factory, row_id, SETTINGS)["with_signals"] == 0       # running again adds nothing


def test_user_without_cash_gets_no_zero_dollar_signal(factory):
    uid = add_user(factory, 1, cash="0")
    with factory.begin() as session:
        data = svc.signals_for(session, uid, make_run(), SETTINGS, NOW)
    assert data["signals"] == [] and "现金" in render_user_signals(data)


def test_held_symbols_carry_no_owner_or_quantity(factory):
    add_user(factory, 1, positions=[("AAA", "5")])
    add_user(factory, 2, positions=[("ZZZ", "1")])
    with factory.begin() as session:
        assert svc.held_symbols(session) == {"AAA", "ZZZ"}


def test_portfolio_preserves_same_ticker_across_exchanges_and_currencies(factory):
    uid = add_user(factory, 1, cash="200", positions=[("AAA", "2")])
    add_user(factory, 2, positions=[("AAA", "999")])
    with factory.begin() as session:
        for exchange, currency, shares, cost in [
            ("NASDAQ", "USD", "3", "50"),
            ("NYSE", "EUR", "7", "20"),
            ("LSE", "USD", "4", "30"),
        ]:
            session.add(Position(user_id=uid, symbol="AAA", exchange=exchange,
                                 currency=currency, shares=Decimal(shares), average_cost=Decimal(cost)))
        session.add(Position(user_id=uid, symbol="FOREIGN", exchange="LSE",
                             currency="USD", shares=Decimal("1"), average_cost=Decimal("10")))
    with factory.begin() as session:
        portfolio = svc.portfolio_of(session, uid)
        assert portfolio.holdings["AAA"].shares == 5
        assert portfolio.holdings["AAA"].avg_price == 70  # (2 * 100 + 3 * 50) / 5
        assert portfolio.holdings["AAA@NYSE:EUR"].shares == 7
        assert portfolio.holdings["AAA@LSE:USD"].shares == 4
        assert len(portfolio.holdings) == 4
        assert svc.held_symbols(session) == {"AAA"}  # foreign tickers are not sent to Alpaca
        data = svc.signals_for(session, uid, make_run(), SETTINGS, NOW)
        assert data["summary"]["equity"] == 830  # cash + US market value + foreign USD cost
        assert any("AAA@NYSE:EUR" in n and "ignored" in n for n in data["notes"])
        assert any("AAA@LSE:USD" in n and "average price" in n for n in data["notes"])
        assert all("@" not in s["symbol"] for s in data["signals"])
        text = svc.check_text(session, uid, svc.store_run(session, make_run())[0], SETTINGS, NOW)
        assert "AAA@LSE:USD" in text and "FOREIGN@LSE:USD" in text


class FakeBot:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_for:
            raise Forbidden("bot was blocked by the user")
        self.sent.append((chat_id, text))


async def test_delivery_sends_one_message_per_user_and_isolates_failures(factory):
    add_user(factory, 1, cash="200")
    add_user(factory, 2, cash="200")
    with factory.begin() as session:
        row, _ = svc.store_run(session, make_run())
        row_id = row.id
    svc.generate_for_all_users(factory, row_id, SETTINGS)
    bot = FakeBot(fail_for={2})
    app = SimpleNamespace(bot=bot, bot_data={"session_factory": factory})
    assert await svc.deliver_pending(app) == (1, 1)
    assert [chat for chat, _ in bot.sent] == [1] and DISCLAIMER in bot.sent[0][1] and "/check" in bot.sent[0][1] and "/signals" not in bot.sent[0][1]
    with factory.begin() as session:
        by_chat = {u.telegram_id: [x for x in session.scalars(select(UserSignal).where(UserSignal.user_id == u.id))] for u in session.scalars(select(User))}
        assert all(x.notified_at for x in by_chat[1])
        assert all(x.notified_at is None and x.notification_attempts == 1 and "Forbidden" in x.last_error for x in by_chat[2])
    app.bot.fail_for.clear()
    assert await svc.deliver_pending(app) == (1, 0)                                          # only the failed user is retried
    assert [chat for chat, _ in bot.sent] == [1, 2]


async def test_retry_job_resumes_after_month_completed_and_restart(factory, monkeypatch):
    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)
    add_user(factory, 1, cash="200")
    add_user(factory, 2, cash="200")
    with factory.begin() as session:
        svc.store_run(session, fresh_run())
    app = SimpleNamespace(bot=FakeBot(fail_for={2}), bot_data={"session_factory": factory})
    result = await svc.run_and_notify(app)
    assert (result["sent"], result["failed"]) == (1, 1)
    with factory.begin() as session:
        assert svc.latest_completed(session).metadata_json["monthly_notified_at"]
    # A fresh application has no in-memory delivery state and autorun is off.
    restarted = SimpleNamespace(bot=FakeBot(), bot_data={"session_factory": factory, "strategy_autorun": False})
    await svc.retry_pending_notifications(SimpleNamespace(application=restarted))
    await svc.retry_pending_notifications(SimpleNamespace(application=restarted))
    assert [chat for chat, _ in restarted.bot.sent] == [2]
    with factory.begin() as session:
        assert all(s.notified_at for s in session.scalars(select(UserSignal)))


async def test_retry_job_stops_at_five_attempts_and_respects_preferences(factory):
    failed = add_user(factory, 1, cash="200")
    disabled = add_user(factory, 2, cash="200")
    with factory.begin() as session:
        row, _ = svc.store_run(session, make_run())
        row_id = row.id
    svc.generate_for_all_users(factory, row_id, SETTINGS)
    with factory.begin() as session:
        session.get(NotificationPreference, disabled).signals_enabled = False
    app = SimpleNamespace(bot=FakeBot(fail_for={1}), bot_data={"session_factory": factory})
    context = SimpleNamespace(application=app)
    for _ in range(7):
        await svc.retry_pending_notifications(context)
    with factory.begin() as session:
        rows = session.scalars(select(UserSignal)).all()
        assert all(s.notification_attempts == 5 for s in rows if s.user_id == failed)
        assert all(s.notification_attempts == 0 for s in rows if s.user_id == disabled)
    assert app.bot.sent == []


async def test_overlapping_delivery_does_not_send_twice(factory):
    import asyncio

    add_user(factory, 1, cash="200")
    with factory.begin() as session:
        row, _ = svc.store_run(session, make_run())
        row_id = row.id
    svc.generate_for_all_users(factory, row_id, SETTINGS)
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowBot(FakeBot):
        async def send_message(self, *args, **kwargs):
            entered.set()
            await release.wait()
            await super().send_message(*args, **kwargs)

    app = SimpleNamespace(bot=SlowBot(), bot_data={"session_factory": factory})
    first = asyncio.create_task(svc.deliver_pending(app))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert await svc.deliver_pending(app) == (0, 0)
    finally:
        release.set()
        await first
    assert len(app.bot.sent) == 1


def test_notification_retry_is_scheduled_independently_of_monthly_job():
    from telegram.ext import Application
    from quantbot.bot import configure_application

    app = configure_application(Application.builder().token("123456:TEST").build(), None, None)
    jobs = {j.name: j for j in app.job_queue.jobs()}
    assert jobs["strategy-notification-retry"].callback is svc.retry_pending_notifications
    assert jobs["monthly-strategy"].callback is svc.monthly_tick


MON = datetime(2026, 11, 2, 2, 30, tzinfo=UTC)          # Monday 2026-11-02 10:30 in UTC+8 = Sunday 21:30 ET: the evening before the US open of the first trading day


def test_monthly_push_window_is_local_daytime_of_a_weekday_and_ignores_check_runs(factory):
    with factory.begin() as session:
        assert svc.due_for_monthly_run(session, MON)                                              # 10:30 UTC+8, nothing sent yet
        assert not svc.due_for_monthly_run(session, datetime(2026, 11, 2, 1, 30, tzinfo=UTC))     # 09:30 UTC+8: before the push hour
        assert not svc.due_for_monthly_run(session, datetime(2026, 10, 31, 2, 30, tzinfo=UTC))    # a Saturday
        svc.store_run(session, make_run("failed", started=MON + timedelta(minutes=15)))
    with factory.begin() as session:
        assert not svc.due_for_monthly_run(session, MON + timedelta(minutes=30))                  # a failed attempt 15 minutes ago: wait an hour
        assert svc.due_for_monthly_run(session, MON + timedelta(hours=2))                         # then retry the same day
        row, _ = svc.store_run(session, make_run("ok", as_of="2026-10-30", started=MON + timedelta(hours=3)))
        row_id = row.id
    with factory.begin() as session:
        assert svc.due_for_monthly_run(session, MON + timedelta(hours=4))                         # a completed run (e.g. from /check) is not the monthly push
        svc.mark_monthly_done(session, row_id, MON + timedelta(hours=4))
    with factory.begin() as session:
        assert not svc.due_for_monthly_run(session, MON + timedelta(hours=5))                     # pushed this month
        assert svc.due_for_monthly_run(session, datetime(2026, 12, 1, 2, 30, tzinfo=UTC))         # next month


def test_first_trading_day_comes_from_the_calendar_and_never_guesses_on_failure():
    from datetime import date

    cal = lambda start, end: [{"date": "2026-11-03"}, {"date": "2026-11-02"}, {"date": "2026-11-04"}]
    assert svc.first_trading_day(SETTINGS, 2026, 11, calendar=cal) == date(2026, 11, 2)
    holiday = lambda start, end: [{"date": "2027-01-04"}, {"date": "2027-01-05"}]                  # Jan 1 is a holiday, Jan 2-3 a weekend
    assert svc.first_trading_day(SETTINGS, 2027, 1, calendar=holiday) == date(2027, 1, 4)

    def broken(start, end):
        raise RuntimeError("calendar down")
    with pytest.raises(svc.StrategyUnavailable):
        svc.first_trading_day(SETTINGS, 2026, 11, calendar=broken)


async def test_monthly_tick_pushes_only_on_the_first_trading_day(factory, accounts, monkeypatch):
    from datetime import date

    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)
    ran = []

    async def fake_run(application, force=False, **kwargs):
        ran.append(1)
        return {"status": "completed"}
    monkeypatch.setattr(svc, "run_and_notify", fake_run)
    app = FakeApp(factory, accounts)
    context = SimpleNamespace(application=app)
    add_user(factory, 1)
    app.bot_data["strategy_autorun"] = False
    monkeypatch.setattr(svc, "month_trading_days", lambda *a, **k: [date(2026, 11, 2), date(2026, 11, 3)])
    await svc.monthly_tick(context, MON)
    assert not ran                                                                                # autorun off
    app.bot_data["strategy_autorun"] = True
    await svc.monthly_tick(context, MON + timedelta(days=1))
    assert not ran                                                                                # Tuesday is not the first trading day
    await svc.monthly_tick(context, MON)
    assert ran == [1]                                                                             # the first trading day, 10:30 UTC+8
    app.bot_data["strategy_push"] = ("Asia/Shanghai", 12)
    await svc.monthly_tick(context, MON)
    assert ran == [1]                                                                             # push hour moved to 12:00


class FakeMessage:
    def __init__(self):
        self.texts = []
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)
        self.replies.append((text, kwargs))


class FakeApp:
    def __init__(self, factory, accounts):
        self.bot, self.tasks = FakeBot(), []
        self.bot_data = {"session_factory": factory, "accounts": accounts}

    def create_task(self, coro, update=None):
        self.tasks.append(coro)

    async def run_tasks(self):
        while self.tasks:
            await self.tasks.pop(0)


def handler_args(factory, accounts, telegram_id, app=None):
    message = FakeMessage()
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=telegram_id), effective_chat=SimpleNamespace(id=telegram_id))
    context = SimpleNamespace(application=app or FakeApp(factory, accounts), args=[], user_data={})
    return update, context, message


def fresh_run():
    return make_run(started=datetime.now(UTC))


async def test_check_shows_ranking_and_suggestions_for_the_caller_only_when_data_is_fresh(factory, accounts, monkeypatch):
    from quantbot import bot

    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)
    add_user(factory, 1, cash="200")
    add_user(factory, 2, cash="200", positions=[("AAA", "5")])
    with factory.begin() as session:
        svc.store_run(session, fresh_run())
    app = FakeApp(factory, accounts)
    update, context, message = handler_args(factory, accounts, 1, app)
    await bot.check(update, context)
    text = message.texts[0]
    assert "2026-10-09" in text and "排名前" in text and "买入" in text and DISCLAIMER in text and "持仓" not in text.split("排名前")[1].split("✅可买入")[0]
    update2, context2, message2 = handler_args(factory, accounts, 2, app)
    await bot.check(update2, context2)
    assert "📌 AAA" in message2.texts[0] and "持仓" in message2.texts[0]                      # user 2 sees their own holding, user 1 did not
    assert not app.tasks and bot.signals is bot.check                                          # fresh data: no refresh, and /signals is an alias
    update3, context3, message3 = handler_args(factory, accounts, 404, app)
    await bot.check(update3, context3)
    assert "/start" in message3.texts[0]


async def test_check_with_stale_data_shows_the_old_result_then_sends_each_waiter_their_own_fresh_one(factory, accounts, monkeypatch):
    from quantbot import bot

    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)
    add_user(factory, 1, cash="200")
    add_user(factory, 2, cash="300")
    add_user(factory, 3, cash="400")                                                          # never asks: must not receive anything
    with factory.begin() as session:
        svc.store_run(session, make_run(started=datetime.now(UTC) - timedelta(days=3)))
    calls = []

    async def fake_refresh(application, force=False):
        calls.append(1)
        with factory.begin() as session:
            svc.store_run(session, make_run(as_of="2026-10-10", started=datetime.now(UTC)))
        return {"status": "completed", "notes": []}
    monkeypatch.setattr(svc, "refresh_run", fake_refresh)
    app = FakeApp(factory, accounts)
    u1, c1, m1 = handler_args(factory, accounts, 1, app)
    u2, c2, m2 = handler_args(factory, accounts, 2, app)
    await bot.check(u1, c1)
    await bot.check(u2, c2)
    assert "不是最新" in m1.texts[0] and "2026-10-09" in m1.texts[0] and len(app.tasks) == 1     # one shared refresh for both askers
    await app.run_tasks()
    assert len(calls) == 1 and sorted(chat for chat, _ in app.bot.sent) == [1, 2]
    sent = dict(app.bot.sent)
    assert "已更新" in sent[1] and "2026-10-10" in sent[1] and "$200" in sent[1] and "$300" in sent[2] and "$300" not in sent[1]
    assert app.bot_data["check_job_running"] is False and app.bot_data["check_waiters"] == {}


async def test_check_does_not_start_another_refresh_right_after_an_attempt(factory, accounts, monkeypatch):
    from quantbot import bot

    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)
    add_user(factory, 1, cash="200")
    with factory.begin() as session:
        svc.store_run(session, make_run("failed", started=datetime.now(UTC) - timedelta(minutes=5)))
    app = FakeApp(factory, accounts)
    update, context, message = handler_args(factory, accounts, 1, app)
    await bot.check(update, context)
    assert "30 分钟" in message.texts[0] and not app.tasks


async def test_check_failure_is_reported_to_the_waiting_user(factory, accounts, monkeypatch):
    from quantbot import bot

    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)
    add_user(factory, 1, cash="200")

    async def broken(application, force=False):
        raise RuntimeError("feed down")
    monkeypatch.setattr(svc, "refresh_run", broken)
    app = FakeApp(factory, accounts)
    update, context, message = handler_args(factory, accounts, 1, app)
    await bot.check(update, context)
    assert "还没有可用的数据" in message.texts[0]
    await app.run_tasks()
    assert app.bot.sent and "更新失败" in app.bot.sent[0][1] and app.bot_data["check_job_running"] is False


async def test_run_and_notify_reuses_a_fresh_run_pushes_to_users_and_marks_the_month(factory, accounts, monkeypatch):
    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)
    add_user(factory, 1, cash="200")
    with factory.begin() as session:
        svc.store_run(session, fresh_run())

    async def must_not_run(*args, **kwargs):
        raise AssertionError("a fresh run must be reused")
    monkeypatch.setattr(svc, "refresh_run", must_not_run)
    app = FakeApp(factory, accounts)
    result = await svc.run_and_notify(app)
    assert result["reused"] and result["sent"] == 1 and app.bot.sent[0][0] == 1
    with factory.begin() as session:
        assert not svc.due_for_monthly_run(session, datetime.now(UTC))


def test_help_topics_exist_fit_telegram_and_cover_every_registered_command():
    from telegram.ext import Application, CommandHandler

    from quantbot import bot, help_text

    for topic in ("导入", "问题", "csv"):
        assert help_text.help_for(topic) != help_text.QUICK
    assert all("没有这个主题" in help_text.help_for(x) for x in ("评分", "规则")) and not hasattr(help_text, "SCORE") and not hasattr(help_text, "RULES")      # neither the scoring method nor the rules are explained
    assert help_text.help_for(None) == help_text.QUICK and "没有这个主题" in help_text.help_for("nope")
    assert all(len(t) < 4096 for t in (help_text.QUICK, help_text.CSV, help_text.FAQ))
    app = bot.configure_application(Application.builder().token("123456:TEST").build(), None, None)
    commands = {c for handler in app.handlers[0] for c in getattr(handler, "commands", ())}
    admin = {"users", "stats", "health", "runstrategy", "announce", "whitelist_add", "whitelist_remove",
             "admin_help", "admin_list", "admin_show", "admin_add", "admin_edit", "admin_remove",
             "start", "cancel", "help", "signals"}
    assert {"check", "cooldown"} <= commands
    missing = [c for c in sorted(commands - admin) if f"/{c}" not in help_text.QUICK]
    assert not missing, f"/help does not mention: {missing}"


async def test_cooldown_command_shows_only_the_callers_own_rules(factory, accounts):
    from quantbot import bot
    from quantbot.services import PortfolioService

    uid = add_user(factory, 1, cash="200", positions=[("AAA", "5")])
    add_user(factory, 2, cash="200")
    update, context, message = handler_args(factory, accounts, 1)
    await bot.cooldown(update, context)
    assert "目前没有冷却期" in message.texts[0]
    run = make_run()
    with factory.begin() as session:
        row, _ = svc.store_run(session, run)
        ref = svc._reference_signal(session, row, run, "AAA")
        session.add(UserSignal(user_id=uid, signal_id=ref.id, action=svc.SignalAction.SELL, recommendation="x", created_at=datetime.now(UTC)))
    await bot.cooldown(update, context)
    assert "目前没有冷却期" in message.texts[1]  # a suggestion alone is never a sale
    with factory.begin() as session:
        PortfolioService().update_position(session, session.get(User, uid), "AAA", shares="3", actor=1, record_sale=True)
    await bot.cooldown(update, context)
    assert "AAA" in message.texts[2] and "到 20" not in message.texts[2] and "天" not in message.texts[2]
    update2, context2, message2 = handler_args(factory, accounts, 2)
    await bot.cooldown(update2, context2)
    assert "AAA" not in message2.texts[0]
    update3, context3, message3 = handler_args(factory, accounts, 404)
    await bot.cooldown(update3, context3)
    assert "/start" in message3.texts[0]


async def test_start_explains_how_to_bind_and_typed_numbers_get_a_reminder_instead_of_silence(factory, accounts):
    from quantbot import bot

    app = FakeApp(factory, accounts)
    update, context, message = handler_args(factory, accounts, 77, app)
    assert await bot.start(update, context) == bot.CONTACT
    assert "分享我的手机号" in message.texts[0] and "确认框" in message.texts[0] and "手机版" in message.texts[0]
    assert await bot.contact_reminder(update, context) == bot.CONTACT
    assert "不能手动输入" in message.texts[1] and "分享我的手机号" in message.texts[1]


def test_no_rule_details_or_component_values_reach_the_user():
    from quantbot import help_text
    from quantbot.strategy_text import render_check, render_cooldowns, render_user_signals

    run = make_run()
    scores = {s: {"total": 80.0 - i, "gate_ok": i != 1, "p_native": 10.0, "v_native": 20.0, "f_native": 9.0, "d_native": 8.0, "q_native": 5.0} for i, s in enumerate(NAMES)}
    sig = lambda kind, action, sym: {"kind": kind, "action": action, "symbol": sym, "amount_usd": 100.0, "est_shares": 1.0, "score": 80.0, "components": {"p": 1, "v": 2, "f": 3, "d": 4, "q": 5}, "reason": "secret"}
    data = {"status": "ok", "strategy": {"name": "R5_K1A_K5x_ETF5_TECH", "version": "v"}, "summary": {"equity": 1000.0, "cash": 500.0, "target_weight": 0.037, "top_candidates": [("AAA", 80.0)]},
            "signals": [sig("K1A_REPLACE", "SELL", "AAA"), sig("K1A_REPLACE", "BUY", "BBB"), sig("K5X_TRIM", "SELL", "CCC"), sig("K5X_TRIM", "BUY", "DDD"), sig("MONTHLY_BUY", "BUY", "EEE"), sig("MONTHLY_BUY", "BUY", "FFF")],
            "notes": ["no buyable candidate: x"]}
    texts = [render_check(run, scores, {"AAA": 100.0}, data, 0), render_user_signals(data), help_text.QUICK, help_text.CSV, help_text.FAQ,
             render_cooldowns({"AAA": datetime(2030, 1, 1, tzinfo=UTC)}, {"BBB": datetime(2030, 1, 1, tzinfo=UTC)}, datetime(2026, 1, 1, tzinfo=UTC))]
    text = "\n".join(texts)
    for leak in ("secret", "R5", "K1A", "K5x", "ETF5", "标准差", "25%", "180", "365", "3.7%", "目标仓位", "门槛", "P(反转)", "V20", "D8", "按分数分配", "相差不超过", "70/55", "盈利收益率"):
        assert leak not in text, leak


def test_cash_input_is_parsed_forgivingly_and_normalised():
    from decimal import Decimal

    import pytest

    from quantbot.services import parse_money_text

    for text, expected in [("200 USD", (200, "USD")), ("200 usd", (200, "USD")), ("200usd", (200, "USD")), ("  200USD ", (200, "USD")), ("$200", (200, "USD")),
                           ("1,000.5 usd", (1000.5, "USD")), ("USD 200", (200, "USD")), ("200", (200, "USD")), ("200美元", (200, "USD")), ("1，000 美金", (1000, "USD")),
                           ("0", (0, "USD")), ("150.25 aud", (150.25, "AUD")), ("US$ 99", (99, "USD"))]:
        assert parse_money_text(text) == (Decimal(str(expected[0])), expected[1]), text
    for bad in ("", "abc", "-5 USD", "200 USDT", "1e5", "200 USD extra", "USD", "99999999999999999999 USD"):
        with pytest.raises(ValueError):
            parse_money_text(bad)


async def test_cash_command_confirms_the_normalised_value_and_explains_bad_input(factory, accounts):
    from quantbot import bot
    from quantbot.models import CashAccount

    uid = add_user(factory, 1, cash="0")
    app = FakeApp(factory, accounts)
    update, context, message = handler_args(factory, accounts, 1, app)
    update.message.text = "200usd"
    assert await bot.cash_data(update, context) == bot.ConversationHandler.END
    assert "200.00 USD" in message.texts[-1]
    with factory.begin() as session:
        cash = session.get(CashAccount, uid)
        assert cash.balance == Decimal("200") and cash.currency == "USD"
    update.message.text = "两百刀"
    assert await bot.cash_data(update, context) == bot.CASH_DATA and "没看懂" in message.texts[-1]
    update.message.text = "150 aud"
    await bot.cash_data(update, context)
    assert "150.00 AUD" in message.texts[-1] and "只分析美元" in message.texts[-1]


def test_numbers_are_shown_the_way_people_write_them():
    from quantbot.display import money, plain, price, qty

    assert money(Decimal("0E-8")) == "0.00" and money(Decimal("1000.00000000")) == "1,000.00" and money(Decimal("1234567.456")) == "1,234,567.46"
    assert qty(Decimal("0.5500000000")) == "0.55" and qty(Decimal("3.0000000000")) == "3" and qty(Decimal("0E-10")) == "0" and qty(Decimal("0.5500000001")) == "0.5500000001"
    assert price(Decimal("374.21000000")) == "374.21" and price(Decimal("120.00000000")) == "120.00" and price(Decimal("380.12345678")) == "380.12345678" and price(Decimal("0.01230000")) == "0.0123"
    assert plain(Decimal("-0E-8"), 2, 8) == "0.00" and "E" not in plain(Decimal("1E+3"))
    assert qty(Decimal("Infinity")) == "数值异常"
    assert money(Decimal("1E+100")) == "数值异常"


async def test_portfolio_command_and_csv_export_never_show_exponents_or_trailing_zeros(factory, accounts):
    from quantbot import bot
    from quantbot.csv_io import export_portfolio_csv, import_portfolio_csv
    from quantbot.models import User

    uid = add_user(factory, 1, cash="0", positions=[("ISRG", "0.55"), ("NVDA", "3")])
    app = FakeApp(factory, accounts)
    update, context, message = handler_args(factory, accounts, 1, app)
    await bot.portfolio(update, context)
    text = message.texts[0]
    assert "0E" not in text and "<b>0.00 USD</b>" in text and "手动管理" not in text and "/cash" in text and "<pre>" in text
    assert "ISRG" in text and "0.55" in text and "NVDA" in text and "100.00 USD" in text and "0000" not in text
    with factory.begin() as session:
        user = session.get(User, uid)
        csv_text = export_portfolio_csv(session, user)
    assert "E-" not in csv_text and "0000000" not in csv_text and "current_cash,0,0.00 USD" in csv_text and "ISRG,0.55,100.00 USD" in csv_text
    other = add_user(factory, 2, cash="5")
    with factory.begin() as session:
        result = import_portfolio_csv(session, session.get(User, other), csv_text, actor=2)
    assert result.positions_created == 2                                                                  # the export can be imported again as it is
    empty = add_user(factory, 3, cash="1500")
    update3, context3, message3 = handler_args(factory, accounts, 3, app)
    await bot.portfolio(update3, context3)
    assert "<b>1,500.00 USD</b>" in message3.texts[0] and "还没有持仓" in message3.texts[0] and "现金为 0" not in message3.texts[0] and empty


async def test_help_depends_on_whether_the_phone_is_bound(factory, accounts):
    from quantbot import bot, help_text

    add_user(factory, 1, cash="0")
    app = FakeApp(factory, accounts)
    bound, ctx_bound, msg_bound = handler_args(factory, accounts, 1, app)
    await bot.help_command(bound, ctx_bound)
    unbound, ctx_unbound, msg_unbound = handler_args(factory, accounts, 2, app)
    await bot.help_command(unbound, ctx_unbound)
    assert msg_bound.texts[0] == help_text.QUICK_PENDING and "/subscribe" in msg_bound.texts[0]
    assert msg_unbound.texts[0] == help_text.QUICK_NEW and "/start" in help_text.QUICK_NEW and "/add" not in help_text.QUICK_NEW


class FakeQuery:
    def __init__(self, data):
        self.data, self.edits, self.answers = data, [], []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


def callback_args(factory, accounts, telegram_id, data, app, user_data=None):
    query = FakeQuery(data)
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=telegram_id))
    return update, SimpleNamespace(application=app, user_data=user_data if user_data is not None else {}, args=[]), query


async def test_unbind_deletes_only_the_callers_data_after_confirmation_and_allows_binding_again(factory, accounts):
    from sqlalchemy import func

    from quantbot import bot
    from quantbot.models import CashAccount, NotificationPreference, Position, Transaction

    mine = add_user(factory, 1, cash="500", positions=[("AAA", "5")])
    other = add_user(factory, 2, cash="300", positions=[("BBB", "2")])
    with factory.begin() as session:
        run = make_run()
        row, _ = svc.store_run(session, run)
        ref = svc._reference_signal(session, row, run, "AAA")
        session.add(UserSignal(user_id=mine, signal_id=ref.id, action=svc.SignalAction.BUY, recommendation="x"))
        session.add(UserSignal(user_id=other, signal_id=ref.id, action=svc.SignalAction.BUY, recommendation="x"))
    app = FakeApp(factory, accounts)
    app.bot_data["check_waiters"] = {mine: 1, other: 2}
    update, context, message = handler_args(factory, accounts, 1, app)
    await bot.unbind_start(update, context)
    assert "不能撤销" in message.texts[0] and "/export" in message.texts[0]
    cancel_data, confirm_data = [button.callback_data for button in message.replies[0][1]["reply_markup"].inline_keyboard[0]][::-1]
    with factory.begin() as session:
        assert session.get(User, mine) is not None                                           # asking is not enough
    u, c, q = callback_args(factory, accounts, 1, cancel_data, app, context.user_data)
    await bot.unbind_confirm(u, c)
    assert "已取消" in q.edits[0]
    with factory.begin() as session:
        assert session.get(User, mine) is not None
    update, context, message = handler_args(factory, accounts, 1, app)
    await bot.unbind_start(update, context)
    confirm_data = message.replies[0][1]["reply_markup"].inline_keyboard[0][0].callback_data
    u, c, q = callback_args(factory, accounts, 1, confirm_data, app, context.user_data)
    await bot.unbind_confirm(u, c)
    assert "已解绑" in q.edits[0] and c.user_data == {} and app.bot_data["check_waiters"] == {other: 2}
    with factory.begin() as session:
        count = lambda model, col: session.scalar(select(func.count()).select_from(model).where(col == mine))
        assert session.get(User, mine) is None
        assert count(Position, Position.user_id) == count(CashAccount, CashAccount.user_id) == count(UserSignal, UserSignal.user_id) == count(NotificationPreference, NotificationPreference.user_id) == count(Transaction, Transaction.user_id) == 0
        assert session.get(User, other) is not None and session.scalar(select(func.count()).select_from(Position).where(Position.user_id == other)) == 1       # nobody else is touched
        assert session.scalar(select(func.count()).select_from(UserSignal).where(UserSignal.user_id == other)) == 1
    update, context, message = handler_args(factory, accounts, 1, app)
    await bot.portfolio(update, context)
    assert "/start" in message.texts[0]                                                      # no longer registered
    with factory.begin() as session:
        assert accounts.register(session, 1, "+61400000001").user is not None               # and can bind again
    u, c, q = callback_args(factory, accounts, 99, confirm_data, app)
    await bot.unbind_confirm(u, c)
    assert "已失效" in q.answers[0][0][0]                                                          # an unregistered person pressing the button deletes nothing


def _assert_valid_telegram_html(text):
    from html.parser import HTMLParser

    allowed, stack = {"b", "i", "code", "pre"}, []

    class Check(HTMLParser):
        def handle_starttag(self, tag, attrs):
            assert tag in allowed and not attrs, tag
            stack.append(tag)

        def handle_endtag(self, tag):
            assert stack and stack.pop() == tag, f"unbalanced </{tag}>"

    parser = Check(convert_charrefs=False)
    parser.feed(text)
    parser.close()
    assert not stack, f"unclosed {stack}"
    assert len(text) < 4096
    stripped = __import__("re").sub(r"</?(?:b|i|code|pre)>", "", text)
    assert "<" not in stripped and ">" not in stripped and "&" not in __import__("re").sub(r"&(?:lt|gt|amp|quot|#x27);", "", stripped)       # everything dynamic is escaped


async def test_portfolio_pages_cover_all_positions_and_buttons_are_account_bound(factory, accounts):
    from quantbot import bot
    from quantbot.display import render_portfolio_pages

    user_id = add_user(factory, 999, cash="500")
    with factory.begin() as session:
        session.add_all(Position(user_id=user_id, symbol=f"STOCK{i:04d}", exchange="US",
                                 shares=Decimal(1), average_cost=Decimal(100), currency="USD")
                        for i in range(200))
    with factory.begin() as session:
        positions = session.scalars(select(Position).where(Position.user_id == user_id).order_by(Position.symbol)).all()
        pages = render_portfolio_pages(session.get(CashAccount, user_id), positions)
    assert len(pages) > 1
    assert all(len(page) <= 4096 for page in pages)
    assert all(sum(symbol in page for page in pages) == 1 for symbol in ("STOCK0000", "STOCK0100", "STOCK0199"))
    for page in pages:
        _assert_valid_telegram_html(page)

    app = FakeApp(factory, accounts)
    update, context, message = handler_args(factory, accounts, 999, app)
    await bot.portfolio(update, context)
    next_data = message.replies[0][1]["reply_markup"].inline_keyboard[0][0].callback_data
    callback_update, callback_context, query = callback_args(factory, accounts, 999, next_data, app)
    await bot.portfolio_page(callback_update, callback_context)
    assert "第 2/" in query.edits[0]

    with factory.begin() as session:
        accounts.unregister(session, 999)
        accounts.register(session, 999, "+61400000999")
    old_update, old_context, old_query = callback_args(factory, accounts, 999, next_data, app)
    await bot.portfolio_page(old_update, old_context)
    assert "旧账户" in old_query.answers[0][0][0]


def test_every_formatted_message_is_valid_telegram_html_even_with_hostile_text():
    from quantbot import help_text
    from quantbot.display import render_portfolio
    from quantbot.strategy_text import render_check, render_cooldowns, render_user_signals

    for text in (help_text.QUICK, help_text.QUICK_NEW, help_text.CSV, help_text.FAQ):
        _assert_valid_telegram_html(text)
    evil = "A<b>&"
    run = make_run()
    scores = {s: {"total": 80.0 - i, "gate_ok": True, "p_native": 10.0, "v_native": 20.0, "f_native": 9.0, "d_native": 8.0, "q_native": 5.0} for i, s in enumerate(NAMES)}
    scores[evil] = dict(scores["AAA"], total=99.0)
    sig = {"kind": "MONTHLY_BUY", "action": "BUY", "symbol": evil, "amount_usd": 100.0, "est_shares": 1.0, "score": 99.0, "components": {}}
    data = {"status": "ok", "strategy": {"name": "x", "version": "v<1>"}, "summary": {"equity": 1000.0, "cash": 500.0, "target_weight": 0.037, "top_candidates": [(evil, 99.0)]}, "signals": [sig],
            "notes": [f"{evil}: amount in EUR ignored"]}
    _assert_valid_telegram_html(render_check(run, scores, {evil: 100.0}, data, 0))
    _assert_valid_telegram_html(render_user_signals(data))
    _assert_valid_telegram_html(render_cooldowns({evil: datetime(2030, 1, 1, tzinfo=UTC)}, {"BBB": datetime(2030, 1, 1, tzinfo=UTC)}, datetime(2026, 1, 1, tzinfo=UTC)))
    cash = SimpleNamespace(balance=Decimal("1500.5"), currency="USD")
    positions = [SimpleNamespace(symbol=evil, exchange="US", shares=Decimal("0.55"), average_cost=Decimal("374.21"), currency="USD"),
                 SimpleNamespace(symbol="BHP", exchange="ASX", shares=Decimal("10"), average_cost=Decimal("40"), currency="AUD")]
    rendered = render_portfolio(cash, positions)
    _assert_valid_telegram_html(rendered)
    _assert_valid_telegram_html(render_portfolio(cash, []))
    assert "BHP@ASX" in rendered and "（" not in rendered.split("</b>")[1].split("\n")[0]


def test_help_shortcuts_are_real_telegram_commands():
    import re

    from telegram.ext import Application

    from quantbot import bot, help_text

    app = bot.configure_application(Application.builder().token("123456:TEST").build(), None, None)
    commands = {c for handler in app.handlers[0] for c in getattr(handler, "commands", ())}
    assert {"help_import", "help_faq"} <= commands and all(re.fullmatch(r"[a-z0-9_]{1,32}", c) for c in commands)
    assert "/help_import" in help_text.QUICK and "/help_faq" in help_text.QUICK and "/help 导入" not in help_text.QUICK


async def test_help_shortcut_commands_show_their_topic(factory, accounts):
    from quantbot import bot, help_text

    add_user(factory, 1, cash="0")
    with factory.begin() as session:
        from quantbot.models import PhoneWhitelist
        session.add(PhoneWhitelist(phone_fingerprint="fp1", created_by_telegram_id=999))
    app = FakeApp(factory, accounts)
    update, context, message = handler_args(factory, accounts, 1, app)
    await bot.help_import(update, context)
    await bot.help_faq(update, context)
    assert message.texts == [help_text.CSV, help_text.FAQ]


async def test_check_still_works_after_the_monthly_push_has_been_recorded(factory, accounts, monkeypatch):
    from quantbot import bot

    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)
    add_user(factory, 1, cash="200")
    with factory.begin() as session:
        row, _ = svc.store_run(session, fresh_run())
        svc.mark_monthly_done(session, row.id)
    with factory.begin() as session:
        assert svc.result_of(svc.latest_completed(session)).status == "ok"
    app = FakeApp(factory, accounts)
    update, context, message = handler_args(factory, accounts, 1, app)
    await bot.check(update, context)
    assert "策略检查" in message.texts[0] and not app.tasks
    app.bot_data["session_factory"] = factory
    result = await svc.run_and_notify(app)                                       # reuses the stored run although it now carries the marker
    assert result["reused"] and result["status"] == "completed"


def test_users_see_ranks_never_scores():
    from quantbot.strategy_text import render_check, render_user_signals, signal_line

    run = make_run()
    scores = {s: {"total": 91.7 - 3.1 * i, "gate_ok": i != 1, "p_native": 10.0, "v_native": 20.0, "f_native": 9.0, "d_native": 8.0, "q_native": 5.0} for i, s in enumerate(NAMES)}
    sig = {"kind": "MONTHLY_BUY", "action": "BUY", "symbol": "CCC", "amount_usd": 100.0, "est_shares": 1.0, "score": 85.6, "rank": 3, "components": {}}
    data = {"status": "ok", "strategy": {"name": "x", "version": "v"}, "summary": {"equity": 1000.0, "cash": 500.0, "top_candidates": [("AAA", 91.7), ("CCC", 85.5)]}, "signals": [sig], "notes": []}
    text = render_check(run, scores, {"EEE": 100.0}, data, 0) + render_user_signals(data) + signal_line(sig)
    for hidden in ("91.7", "88.6", "85.5", "85.6", "79.3", "75.5", "评分"):
        assert hidden not in text, hidden
    assert " 1  ✅ AAA" in text and "排名前" in text
    assert all(hidden not in text for hidden in ("排名第 3", "AAA › CCC", "本月买入", "排名靠前的可买入"))
    assert " 5  📌 EEE" in text and "持仓" in text                                      # in the overall top table
    assert " 1  📌 EEE" in text and "全体第" not in text                                  # in the holdings list the first holding is number 1


def test_every_command_in_the_help_comes_with_how_to_use_it():
    from quantbot import help_text

    quick = help_text.QUICK
    for usage in ("<code>ISRG 0.55 374.21 USD</code>", "<code>ISRG 0.75 380</code>", "<code>/remove ISRG</code>", "<code>1000 USD</code>", "<code>/settings signals</code>", "<code>/settings announcements</code>", "再把 CSV 文件发给我", "直接发送，会收到一份 CSV", "发送后点确认", "输入过程中随时取消"):
        assert usage in quick, usage
    for command in ("/check", "/cooldown", "/portfolio", "/history", "/add", "/edit", "/remove", "/cash", "/import", "/export", "/settings", "/unbind", "/cancel"):
        line = next(x for x in quick.splitlines() if x.startswith(command + "　"))
        assert len(line) > len(command) + 4, command                                    # a description follows every command


def test_holdings_are_numbered_among_themselves_starting_at_one():
    from quantbot.strategy_text import render_check

    run = make_run()
    many = [f"S{i:02d}" for i in range(30)]
    scores = {s: {"total": 90.0 - i, "gate_ok": True, "p_native": 0, "v_native": 0, "f_native": 0, "d_native": 0, "q_native": 0} for i, s in enumerate(many)}
    held = {"S25": 100.0, "S03": 300.0, "S12": 200.0}                                   # overall ranks 26, 4 and 13
    data = {"status": "ok", "strategy": {"name": "x", "version": "v"}, "summary": {"equity": 1000.0, "cash": 400.0, "top_candidates": []}, "signals": [], "notes": []}
    section = render_check(run, scores, held, data, 0).split("你的持仓排名")[1].split("</pre>")[0].replace("<pre>", "")
    lines = [x for x in section.splitlines() if "📌" in x]
    assert [x.split()[0] for x in lines] == ["1", "2", "3"] and [x.split()[2] for x in lines] == ["S03", "S12", "S25"]
    assert all("全体第" not in x for x in lines)


def test_plan_command_is_gone_and_the_wording_is_the_agreed_one():
    from telegram.ext import Application

    from quantbot import bot, help_text
    from quantbot.strategy_text import DISCLAIMER, render_user_signals

    app = bot.configure_application(Application.builder().token("123456:TEST").build(), None, None)
    commands = {c for handler in app.handlers[0] for c in getattr(handler, "commands", ())}
    assert "plan" not in commands and not [j for j in (app.job_queue.jobs() if app.job_queue else []) if "plan" in j.name]
    assert "/plan" not in help_text.QUICK and "定投计划" not in help_text.QUICK + help_text.FAQ
    assert DISCLAIMER == "仅供参考，不构成投资建议；系统不会下单，买卖由你自己决定。"
    data = {"status": "ok", "strategy": {"name": "x", "version": "abc123"}, "summary": {"equity": 1.0, "cash": 1.0, "top_candidates": []}, "signals": [], "notes": []}
    shown = render_user_signals(data) + help_text.QUICK + help_text.QUICK_NEW
    assert "策略版本" not in shown and "abc123" not in shown and "仅为建议" not in shown and "只是建议" not in shown and "仅供参考，不构成投资建议" in shown

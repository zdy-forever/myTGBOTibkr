import calendar
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from quantbot import bot, strategy_service as svc
from quantbot.models import NotificationPreference, UserSignal
from tests.test_strategy_service import SETTINGS, FakeApp, add_user, fresh_run, handler_args, make_run


def weekdays(start, end):
    return [{"date": str(start + timedelta(days=i))} for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]


@pytest.mark.parametrize("value", [None, True, False, 5, 5.0, "", "0", "-1", "+2", "2.5", "1e1", "25", "24", "100", "五", "５", "5 6", "9" * 10000])
def test_notice_day_rejects_invalid_types_and_ranges(value):
    with pytest.raises(ValueError):
        svc.parse_notice_day(value)


@pytest.mark.parametrize("value,expected", [("1", 1), ("01", 1), ("19", 19), ("23", 23)])
def test_notice_day_accepts_valid_integers(value, expected):
    assert svc.parse_notice_day(value) == expected


@pytest.mark.parametrize("year,length", [(2027, 28), (2028, 29)])
def test_calendar_uses_whole_february_deduplicates_and_excludes_holidays(year, length):
    calls = []

    def fake(start, end):
        calls.append((start, end))
        days = weekdays(start, end)
        return days[1:] + days[1:2] + [{"date": str(end + timedelta(days=1))}]

    days = svc.month_trading_days(SETTINGS, year, 2, fake)
    assert calls == [(date(year, 2, 1), date(year, 2, length))]
    assert days == sorted(set(days)) and all(d.month == 2 for d in days)
    assert days[0] > date.fromisoformat(weekdays(*calls[0])[0]["date"])


def test_preview_explains_this_next_month_first_failure_and_all_short_months():
    text = svc.notice_date_preview(SETTINGS, date(2027, 1, 1), 21, calendar=weekdays)
    assert "本月（2027-01）：可生效，对应 2027-01-29" in text
    assert "下个月（2027-02）：不能生效，只有 20 个交易日" in text
    assert "首次确定无法生效的月份：2027-02" in text
    assert "2027-02（20 个交易日）" in text
    assert "2027-12 之后尚未核实" in text
    assert "后续符合条件的月份会恢复通知" in text
    assert len(text) < 4096


def test_preview_marks_missing_calendar_months_unknown_and_does_not_claim_unlimited_validity():
    def partial(start, end):
        return weekdays(start, date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]))

    text = svc.notice_date_preview(SETTINGS, date(2027, 1, 1), 5, calendar=partial)
    assert "本月（2027-01）：可生效" in text
    assert "下个月（2027-02）：日历数据缺失，尚未确认" in text
    assert "尚不能确定第一次失效" in text
    assert "未来 12 个月均有" not in text
    failed = svc.notice_date_preview(SETTINGS, date(2027, 1, 1), 5, calendar=lambda *a: [])
    assert "本月：交易日历暂不可用" in failed and "下个月：交易日历暂不可用" in failed


def test_preview_handles_past_day_completed_month_and_year_rollover():
    text = svc.notice_date_preview(SETTINGS, date(2027, 12, 20), 1, calendar=weekdays)
    assert "2027-12-01 已过，本月不补发" in text and "下个月（2028-01）" in text
    text = svc.notice_date_preview(SETTINGS, date(2027, 12, 1), 5, completed="2027-12", calendar=weekdays)
    assert "本月已完成通知任务，不重复发送" in text
    assert "未来 12 个月均有这个交易日" in text


async def test_command_only_changes_self_and_shows_preview_and_disabled_flags(factory, accounts, monkeypatch):
    uid = add_user(factory, 1, signals_enabled=False)
    other = add_user(factory, 2)
    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)
    calls = []

    def preview(*args):
        calls.append(args)
        return "本月：可生效。下个月：不能生效。首次无法生效：下个月。"

    monkeypatch.setattr(svc, "notice_date_preview", preview)
    update, context, message = handler_args(factory, accounts, 1)
    context.args = ["22"]
    await bot.auto_notice_date(update, context)
    with factory.begin() as session:
        assert session.get(NotificationPreference, uid).auto_notice_day == 22
        assert session.get(NotificationPreference, other).auto_notice_day == 1
    assert calls[0][2] == 22
    assert "本月：可生效。下个月：不能生效" in message.texts[-1]
    assert "2 月即使是闰年" in message.texts[-1]
    assert "你的策略通知目前已关闭" in message.texts[-1]
    assert "系统自动推送尚未开启" in message.texts[-1]
    context.args = []
    await bot.auto_notice_date(update, context)
    assert "当前设置：每月第 22" in message.texts[-1]
    for invalid in (["25"], ["2.5"], ["5", "6"]):
        context.args = invalid
        await bot.auto_notice_date(update, context)
    assert len(calls) == 2
    with factory.begin() as session:
        assert session.get(NotificationPreference, uid).auto_notice_day == 22


async def test_command_rejects_unregistered_user_and_saves_if_calendar_unavailable(factory, accounts, monkeypatch):
    update, context, message = handler_args(factory, accounts, 99)
    context.args = ["5"]
    await bot.auto_notice_date(update, context)
    assert "/start" in message.texts[-1]
    uid = add_user(factory, 99)

    def unavailable():
        raise svc.StrategyUnavailable("missing credentials")

    monkeypatch.setattr(svc, "strategy_settings", unavailable)
    await bot.auto_notice_date(update, context)
    assert "本月、下个月及后续月份" in message.texts[-1] and "尚未确认" in message.texts[-1]
    with factory.begin() as session:
        assert session.get(NotificationPreference, uid).auto_notice_day == 5


async def test_scheduler_targets_only_due_users_and_survives_restart_and_date_change(factory, accounts, monkeypatch):
    a = add_user(factory, 1, cash="200")
    b = add_user(factory, 2, cash="200")
    c = add_user(factory, 3, cash="200", signals_enabled=False)
    with factory.begin() as session:
        session.get(NotificationPreference, b).auto_notice_day = 2
        svc.store_run(session, fresh_run())
    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)
    monkeypatch.setattr(svc, "month_trading_days", lambda *a: [date(2026, 11, 2), date(2026, 11, 3)])
    app = FakeApp(factory, accounts)
    app.bot_data["strategy_autorun"] = True
    first = datetime(2026, 11, 2, 2, 30, tzinfo=UTC)
    await svc.monthly_tick(SimpleNamespace(application=app), first)
    assert [chat for chat, _ in app.bot.sent] == [1]
    with factory.begin() as session:
        assert session.get(NotificationPreference, a).last_notice_month == "2026-11"
        assert session.get(NotificationPreference, b).last_notice_month is None
        assert session.get(NotificationPreference, c).last_notice_month is None
        session.get(NotificationPreference, a).auto_notice_day = 2
    restarted = FakeApp(factory, accounts)
    restarted.bot_data["strategy_autorun"] = True
    await svc.monthly_tick(SimpleNamespace(application=restarted), first + timedelta(days=1))
    await svc.monthly_tick(SimpleNamespace(application=restarted), first + timedelta(days=1, hours=1))
    assert [chat for chat, _ in restarted.bot.sent] == [2]
    with factory.begin() as session:
        assert {s.user_id for s in session.scalars(select(UserSignal))} == {a, b}


async def test_scheduler_attempt_limit_is_per_user_persistent_and_does_not_send_on_missing_day(factory, accounts, monkeypatch):
    uid = add_user(factory, 1)
    short = add_user(factory, 2)
    with factory.begin() as session:
        session.get(NotificationPreference, short).auto_notice_day = 23
    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)
    monkeypatch.setattr(svc, "month_trading_days", lambda *a: [date(2027, 2, 1)])
    calls = []

    async def failed(app, **kwargs):
        calls.append(kwargs["user_ids"])
        return {"status": "failed"}

    monkeypatch.setattr(svc, "run_and_notify", failed)
    first = datetime(2027, 2, 1, 2, 30, tzinfo=UTC)
    for minutes in (0, 30, 60, 120, 180, 240):
        app = FakeApp(factory, accounts)  # persisted cap works across process restarts
        app.bot_data["strategy_autorun"] = True
        await svc.monthly_tick(SimpleNamespace(application=app), first + timedelta(minutes=minutes))
    assert calls == [[uid]] * 3
    with factory.begin() as session:
        assert session.get(NotificationPreference, uid).notice_attempts == 3
        assert session.get(NotificationPreference, short).notice_attempts == 0


async def test_scheduler_never_guesses_when_calendar_fails(factory, accounts, monkeypatch):
    uid = add_user(factory, 1)
    monkeypatch.setattr(svc, "strategy_settings", lambda: SETTINGS)

    def broken(*args):
        raise svc.StrategyUnavailable("calendar unavailable")

    monkeypatch.setattr(svc, "month_trading_days", broken)
    app = FakeApp(factory, accounts)
    app.bot_data["strategy_autorun"] = True
    await svc.monthly_tick(SimpleNamespace(application=app), datetime(2027, 2, 1, 2, 30, tzinfo=UTC))
    assert not app.bot.sent
    with factory.begin() as session:
        assert session.get(NotificationPreference, uid).notice_attempts == 0


def test_generation_does_not_mark_failed_user_complete_or_include_other_dates(factory, monkeypatch):
    a = add_user(factory, 1)
    b = add_user(factory, 2)
    c = add_user(factory, 3)
    with factory.begin() as session:
        session.get(NotificationPreference, c).auto_notice_day = 2
        row, _ = svc.store_run(session, make_run())
        row_id = row.id
    original = svc.signals_for

    def fail_one(session, uid, *args):
        if uid == a:
            raise RuntimeError("one user failed")
        return original(session, uid, *args)

    monkeypatch.setattr(svc, "signals_for", fail_one)
    result = svc.generate_for_all_users(factory, row_id, SETTINGS, user_ids=[a, b, c], notice_month="2026-11", notice_day=1)
    assert result["errors"] == 1
    with factory.begin() as session:
        assert session.get(NotificationPreference, a).last_notice_month is None
        assert session.get(NotificationPreference, b).last_notice_month == "2026-11"
        assert session.get(NotificationPreference, c).last_notice_month is None


def test_migration_preserves_existing_preferences_and_completed_month(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(cfg, "0001_initial")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id,telegram_id,phone_fingerprint,role,is_active,created_at,updated_at) VALUES ('u',1,'fp','USER',1,'2026-01-01','2026-01-01')"))
        conn.execute(text("INSERT INTO notification_preferences (user_id,signals_enabled,announcements_enabled,timezone) VALUES ('u',1,0,'UTC')"))
        conn.execute(text("INSERT INTO strategy_runs (trading_date,strategy_version,status,started_at,metadata_json) VALUES ('2026-10-01','v','COMPLETED','2026-10-01',:data)"),
                     {"data": '{"monthly_notified_at":"2026-10-01T02:00:00+00:00"}'})
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        row = conn.execute(text("SELECT * FROM notification_preferences")).mappings().one()
        assert row["auto_notice_day"] == 1 and row["last_notice_month"] == "2026-10"
        assert row["notice_attempts"] == 0 and row["announcements_enabled"] == 0
    command.downgrade(cfg, "0001_initial")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT user_id FROM notification_preferences")).scalar() == "u"
    engine.dispose()

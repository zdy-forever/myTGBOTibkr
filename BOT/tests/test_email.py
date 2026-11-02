from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from quantbot import daily_service as daily, email_commands, email_service, email_templates
from quantbot import strategy_service as s1
from quantbot.allocations import set_strategy_cash
from quantbot.db import transaction
from quantbot.models import DailyReport, EmailDelivery, EmailSubscription, User, UserStrategy
from sgs.live import TICKERS
from tests.test_admin_management import Message, context, update
from tests.test_strategy_service import SETTINGS, add_user

TODAY = date(2026, 11, 3)


class Outbox:
    def __init__(self, fail=0):
        self.sent, self.fail = [], fail

    def __call__(self, config, receiver, subject, body):
        if self.fail:
            self.fail -= 1
            raise OSError("smtp down")
        self.sent.append((receiver, subject, body))


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(email_service, "send", box)
    monkeypatch.setattr(email_commands, "send", box)
    return box


def app(factory, accounts=None, smtp=True):
    return SimpleNamespace(bot_data={"session_factory": factory, "accounts": accounts,
                                     "smtp": email_service.SmtpConfig("s@qq.com", "pw", "smtp.qq.com", 465) if smtp else None})


def subscribe(factory, uid, email="a@qq.com", strategies="1,2"):
    with factory.begin() as session:
        session.add(EmailSubscription(email=email, user_id=uid, strategies=strategies, created_by=999))


def report(factory, uid, strategy_id, *, body="<b>卖出 TQQQ</b> 1 股 &amp; 更多", acts=True, ready=True):
    with factory.begin() as session:
        session.add(DailyReport(user_id=uid, strategy_id=strategy_id, session_date=TODAY, body=body,
                                data_ready=ready, has_actions=acts))


@pytest.mark.parametrize("raw,expected", [("1,2", "1,2"), ("2，1", "1,2"), ("2 2 1", "1,2"), ("2", "2")])
def test_parse_strategies_accepts_lists(raw, expected):
    assert email_service.parse_strategies(raw) == expected


@pytest.mark.parametrize("raw", ["", "3", "1,x", "1,,9"])
def test_parse_strategies_rejects_unknown(raw):
    with pytest.raises(ValueError):
        email_service.parse_strategies(raw)


def test_normalize_email():
    assert email_service.normalize_email(" A.B@QQ.com ") == "a.b@qq.com"
    for bad in ["", "a@", "a b@qq.com", "@qq.com", "a@qq"]:
        with pytest.raises(ValueError):
            email_service.normalize_email(bad)


def test_compose_uses_template_without_actions_and_report_with_actions():
    subject, body = email_service.compose("2", TODAY, "<b>x</b>", False)
    assert subject == email_templates.NO_ACTION_SUBJECT.format(strategy="2", date="2026-11-03")
    assert body == email_templates.NO_ACTION_BODY.format(strategy="2", date="2026-11-03")
    subject, body = email_service.compose("2", TODAY, "<b>卖出</b> A &amp; B<br>下一行", True)
    assert subject == email_templates.ACTION_SUBJECT.format(strategy="2", date="2026-11-03")
    assert body == "卖出 A & B\n下一行\n"


def test_smtp_config_requires_sender_and_password(monkeypatch):
    monkeypatch.delenv("EMAIL_SENDER", raising=False)
    monkeypatch.delenv("EMAIL_PASSWORD", raising=False)
    assert email_service.SmtpConfig.from_env() is None
    monkeypatch.setenv("EMAIL_SENDER", "s@qq.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "code")
    config = email_service.SmtpConfig.from_env()
    assert (config.host, config.port) == ("smtp.qq.com", 465)


async def test_delivery_per_strategy_once_with_template_or_report(factory, outbox):
    uid = add_user(factory, 1)
    subscribe(factory, uid, strategies="1,2")
    report(factory, uid, "strategy1", acts=False)
    report(factory, uid, "strategy2", acts=True)
    opening = datetime.now(UTC) + timedelta(hours=1)
    await email_service.deliver_emails(app(factory), TODAY, opening)
    assert [(r, s) for r, s, _ in outbox.sent] == [
        ("a@qq.com", "策略 1 · 2026-11-03 · 今日无需操作"),
        ("a@qq.com", "策略 2 · 2026-11-03 · 今日有操作，请查看")]
    assert "卖出 TQQQ 1 股 & 更多" in outbox.sent[1][2]
    await email_service.deliver_emails(app(factory), TODAY, opening)
    assert len(outbox.sent) == 2


async def test_only_subscribed_strategies_and_ready_reports(factory, outbox):
    uid = add_user(factory, 1)
    subscribe(factory, uid, strategies="2")
    report(factory, uid, "strategy1")
    report(factory, uid, "strategy2", ready=False)
    await email_service.deliver_emails(app(factory), TODAY, datetime.now(UTC) + timedelta(hours=1))
    assert outbox.sent == []


async def test_failures_retry_then_stop(factory, monkeypatch):
    box = Outbox(fail=99)
    monkeypatch.setattr(email_service, "send", box)
    uid = add_user(factory, 1)
    subscribe(factory, uid, strategies="2")
    report(factory, uid, "strategy2")
    opening = datetime.now(UTC) + timedelta(hours=1)
    for _ in range(email_service.MAX_ATTEMPTS + 2):
        await email_service.deliver_emails(app(factory), TODAY, opening)
    with factory.begin() as session:
        row = session.scalar(select(EmailDelivery))
        assert row.attempts == email_service.MAX_ATTEMPTS and row.sent_at is None and row.last_error == "OSError"
    box.fail = 0
    with factory.begin() as session:
        session.scalar(select(EmailDelivery)).attempts = 1
    await email_service.deliver_emails(app(factory), TODAY, opening)
    assert len(box.sent) == 1


async def test_no_email_after_open_or_without_smtp(factory, outbox):
    uid = add_user(factory, 1)
    subscribe(factory, uid, strategies="2")
    report(factory, uid, "strategy2")
    await email_service.deliver_emails(app(factory), TODAY, datetime.now(UTC) - timedelta(minutes=1))
    await email_service.deliver_emails(app(factory, smtp=False), TODAY, datetime.now(UTC) + timedelta(hours=1))
    assert outbox.sent == []


async def test_inactive_user_gets_no_email(factory, outbox):
    uid = add_user(factory, 1)
    subscribe(factory, uid, strategies="2")
    report(factory, uid, "strategy2")
    with factory.begin() as session:
        session.get(User, uid).is_active = False
    await email_service.deliver_emails(app(factory), TODAY, datetime.now(UTC) + timedelta(hours=1))
    assert outbox.sent == []


async def test_daily_run_saves_action_flag_and_emails_even_with_telegram_off(factory, monkeypatch, outbox):
    uid = add_user(factory, 1, signals_enabled=False)
    with factory.begin() as session:
        session.delete(session.get(UserStrategy, (uid, "strategy1")))
        session.add(UserStrategy(user_id=uid, strategy_id="strategy2"))
    with factory.begin() as session:
        set_strategy_cash(session, session.get(User, uid), 2, 300, "USD", actor=1)
    subscribe(factory, uid, strategies="2")
    now = datetime.now(UTC)
    monkeypatch.setattr(s1, "strategy_settings", lambda: SETTINGS)
    monkeypatch.setattr(s1, "build_engine", lambda settings: SimpleNamespace(market=object()))
    monkeypatch.setattr(daily, "calendar_window", lambda *a: (TODAY, TODAY - timedelta(days=1), [TODAY], now + timedelta(hours=1)))
    monkeypatch.setattr(daily, "s2_snapshot", lambda *a: {"as_of": "2026-11-02", "trend": "强上涨",
                        "weights": {"TQQQ": .855}, "prices": {t: 100 for t in TICKERS}})
    telegram = AsyncMock()
    application = app(factory)
    application.bot = SimpleNamespace(send_message=telegram)
    await daily._daily_run(application, now)
    with factory.begin() as session:
        row = session.scalar(select(DailyReport))
        assert row.has_actions is True and "买入 TQQQ" in row.body
    telegram.assert_not_awaited()
    assert [s for _, s, _ in outbox.sent] == ["策略 2 · 2026-11-03 · 今日有操作，请查看"]


async def test_admin_commands(factory, accounts, outbox):
    with transaction(factory) as session:
        accounts.register(session, 999, "+61400000999")
        accounts.register(session, 101, "+61400000101")

    async def run(handler, args, telegram_id=999, smtp=True):
        message = Message("/x")
        ctx = context(factory, accounts, args)
        ctx.application.bot_data["smtp"] = app(factory, smtp=smtp).bot_data["smtp"]
        await handler(update(message, telegram_id), ctx)
        return message.replies[-1][0]

    assert "仅已验证的管理员" in await run(email_commands.email_add, ["a@qq.com", "101", "1,2"], telegram_id=101)
    assert "已添加" in await run(email_commands.email_add, ["A@QQ.com", "101", "2,1"])
    assert "已经存在" in await run(email_commands.email_add, ["a@qq.com", "101", "2"])
    assert "找不到" in await run(email_commands.email_add, ["b@qq.com", "555", "2"])
    assert "未知策略" in await run(email_commands.email_add, ["b@qq.com", "101", "7"])
    listing = await run(email_commands.email_list, [])
    assert "a@qq.com" in listing and "<code>101</code>" in listing and "策略1、策略2" in listing
    assert "策略2" in await run(email_commands.email_edit, ["a@qq.com", "2"])
    with factory.begin() as session:
        assert session.get(EmailSubscription, "a@qq.com").strategies == "2"
    assert "测试邮件已发送" in await run(email_commands.email_test, ["a@qq.com"])
    assert outbox.sent[-1][1].startswith("[测试] ")
    assert "未配置" in await run(email_commands.email_test, ["a@qq.com"], smtp=False)
    assert "已删除" in await run(email_commands.email_remove, ["a@qq.com"])
    assert "没有这个邮箱" in await run(email_commands.email_remove, ["a@qq.com"])


async def test_removing_subscription_with_history(factory, outbox):
    uid = add_user(factory, 1)
    subscribe(factory, uid, strategies="2")
    report(factory, uid, "strategy2")
    await email_service.deliver_emails(app(factory), TODAY, datetime.now(UTC) + timedelta(hours=1))
    with factory.begin() as session:
        session.delete(session.get(EmailSubscription, "a@qq.com"))
    with factory.begin() as session:
        assert session.scalar(select(EmailDelivery)) is None


def test_migration_0019_upgrades_and_downgrades(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    command.downgrade(config, "0018_paper_trading")
    command.upgrade(config, "head")

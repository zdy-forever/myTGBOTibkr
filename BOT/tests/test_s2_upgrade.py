"""SCHD20 upgrade must invalidate both caches and legacy portfolio targets."""
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from sgs.live import MANAGED_TICKERS, PROFILE_LABEL, VERSION
from quantbot import daily_service as daily
from quantbot.models import DailyReport, StrategySnapshot, UserStrategy
from tests.test_strategy_service import add_user


def only_s2(factory, user_id):
    with factory.begin() as session:
        session.delete(session.get(UserStrategy, (user_id, 'strategy1')))
        session.add(UserStrategy(user_id=user_id, strategy_id='strategy2'))


@pytest.mark.parametrize('cached_today', [True, False])
@pytest.mark.parametrize('old_version', ['strategy2-G2-20261020', 'strategy2-G2-SCHD20-20261021'])
def test_old_snapshot_replaced_and_never_passed_to_new_classifier(factory, monkeypatch, cached_today, old_version):
    today = date(2026, 10, 20)
    old = {'version': old_version, 'weights': {'GPIQ': 1}}
    with factory.begin() as session:
        session.add(StrategySnapshot(strategy_id='strategy2', session_date=today-timedelta(days=1), data=old))
        if cached_today:
            session.add(StrategySnapshot(strategy_id='strategy2', session_date=today, data=old))
    current = {'version': VERSION, 'weights': {'SCHD': 1}}
    calls = []

    def fresh(market, day, previous):
        calls.append((day, previous))
        return current

    monkeypatch.setattr('sgs.live.snapshot', fresh)
    assert daily.s2_snapshot(factory, object(), today) == current
    assert calls == [(today, None)]
    assert daily.s2_snapshot(factory, object(), today) == current
    assert len(calls) == 1
    with factory.begin() as session:
        rows = list(session.scalars(select(StrategySnapshot).where(StrategySnapshot.session_date == today)))
        assert len(rows) == 1 and rows[0].data == current


@pytest.mark.parametrize('old_version', [None, 'strategy2-G2-SCHD20-20261021'])
async def test_stale_ready_report_is_not_delivered_and_is_recomputed(factory, old_version):
    uid = add_user(factory, 1)
    only_s2(factory, uid)
    now = datetime.now(UTC)
    today = now.date()
    with factory.begin() as session:
        session.add(DailyReport(user_id=uid, strategy_id='strategy2', session_date=today,
                               body='旧版买入建议', strategy_version=old_version))
    app = SimpleNamespace(bot_data={'session_factory': factory}, bot=SimpleNamespace(send_message=AsyncMock()))
    await daily.deliver_reports(app, today, now+timedelta(hours=1))
    app.bot.send_message.assert_not_awaited()
    with factory.begin() as session:
        row = session.scalar(select(DailyReport))
        old_id = row.id
        assert row.attempts == 5 and '旧报告已作废' in row.last_error
        assert daily.needs_report(session, uid, 'strategy2', today, now)
        daily.save_report(session, uid, 'strategy2', today, '新SCHD报告')
        session.flush()
        assert row.id == old_id and row.strategy_version == VERSION
        assert row.attempts == 0 and row.last_error is None
        assert not daily.needs_report(session, uid, 'strategy2', today, now)
    await daily.deliver_reports(app, today, now+timedelta(hours=1))
    assert app.bot.send_message.await_count == 1
    assert app.bot.send_message.await_args.args[1] == '新SCHD报告'


def test_report_counts_allocated_legacy_holdings_and_suggests_sell_then_buy(factory):
    uid = add_user(factory, 1, positions=[('GPIQ', '10')], cash='0')
    only_s2(factory, uid)
    with factory.begin() as session:
        body = daily.s2_text(session, uid, {'as_of': '2026-10-19', 'trend': '震荡',
            'weights': {'SCHD': 1}, 'prices': dict.fromkeys(MANAGED_TICKERS, 100)}, date(2026, 10, 20))
    assert PROFILE_LABEL in body
    assert '卖出 GPIQ 10 股' in body
    assert '买入 SCHD' in body and '买入 GPIQ' not in body
    assert body.index('卖出 GPIQ') < body.index('买入 SCHD')


def test_report_version_migration_preserves_existing_records(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    monkeypatch.setenv('DATABASE_URL', url)
    config = Config(str(root/'alembic.ini'))
    config.set_main_option('script_location', str(root/'alembic'))
    command.upgrade(config, '0015_daily_strategies')
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id,telegram_id,phone_fingerprint,role,is_active,created_at,updated_at) "
                          "VALUES ('old',101,'fp','USER',1,'2026-01-01','2026-01-01')"))
        conn.execute(text("INSERT INTO daily_reports "
                          "(user_id,strategy_id,session_date,body,data_ready,created_at,attempts) "
                          "VALUES ('old','strategy2','2026-10-20','old report',1,'2026-10-20',0)"))
        conn.execute(text("INSERT INTO strategy_snapshots (strategy_id,session_date,data) "
                          "VALUES ('strategy2','2026-10-20',:data)"),
                     {'data': '{"version":"old","weights":{"GPIQ":1}}'})
    command.upgrade(config, 'head')
    with engine.begin() as conn:
        report = conn.execute(text('SELECT * FROM daily_reports')).mappings().one()
        assert report['body'] == 'old report' and report['strategy_version'] is None
        assert conn.execute(text('SELECT count(*) FROM users')).scalar() == 1
        assert 'GPIQ' in conn.execute(text('SELECT data FROM strategy_snapshots')).scalar()
        conn.execute(text('UPDATE daily_reports SET strategy_version=:version'), {'version': VERSION})
    command.downgrade(config, '0015_daily_strategies')
    with engine.begin() as conn:
        assert conn.execute(text('SELECT body FROM daily_reports')).scalar() == 'old report'
    command.upgrade(config, 'head')
    engine.dispose()

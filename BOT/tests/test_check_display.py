from types import SimpleNamespace

from quantbot import strategy_service as svc
from quantbot.models import UserSignal
from quantbot.strategy_text import render_check, render_user_signals
from quant_strategy.signals import user_scores
from tests.test_strategy_service import SETTINGS, FakeBot, add_user, fresh_run, make_run


def test_check_uses_beijing_completion_time_and_stock_only_percentages():
    run = make_run()
    run.started_at = '2026-10-10T15:44:00+00:00'
    run.finished_at = '2026-10-10T16:04:00+00:00'
    scores = user_scores(run, {'AAA': 300, 'BBB': 100})
    data = {'status': 'no_candidates', 'summary': {'equity': 1000, 'cash': 600, 'buyable_symbols': []}, 'signals': [], 'notes': []}
    text = render_check(run, scores, {'AAA': 300, 'BBB': 100}, data, 0)
    assert '2026-10-11 00:04（北京时间）' in text and ' UTC' not in text
    assert '持仓75%' in text and '持仓25%' in text and '持仓30%' not in text
    assert '现金 $600' in text  # cash is still reported separately


def test_buy_flags_follow_actual_candidates_without_disclosing_rules():
    run = make_run()
    scores = user_scores(run, {})
    data = {'status': 'no_candidates', 'summary': {'equity': 100, 'cash': 100, 'buyable_symbols': ['AAA']}, 'signals': [], 'notes': []}
    text = render_check(run, scores, {}, data, 0)
    assert '✅ AAA' in text and '🔒 BBB' in text
    assert '不等于冷却期' in text
    assert all(secret not in text for secret in ('GATE', 'P3', '门槛', '180', '365'))


def test_own_outside_holdings_appear_only_in_holdings_ranking(factory):
    uid = add_user(factory, 1, cash='200', positions=[('LMT', '5')])
    other = add_user(factory, 2, cash='200')
    run = make_run()
    run.holding_symbols = {'LMT': dict(run.symbols['AAA']), 'SECRET': dict(run.symbols['BBB'])}
    run.prices.update(LMT=100, SECRET=100)
    with factory.begin() as session:
        row, _ = svc.store_run(session, run)
        mine = svc.check_text(session, uid, row, SETTINGS)
        theirs = svc.check_text(session, other, row, SETTINGS)
    assert 'LMT' not in mine.split('你的持仓排名')[0]
    assert 'LMT' in mine.split('你的持仓排名')[1] and '持仓100%' in mine
    assert 'SECRET' not in mine and 'LMT' not in theirs and 'SECRET' not in theirs


def test_cached_run_refreshes_when_a_new_holding_needs_scoring(factory):
    uid = add_user(factory, 1, positions=[('LMT', '5')])
    run = fresh_run()
    with factory.begin() as session:
        row, _ = svc.store_run(session, run)
        row_id = row.id
        svc.mark_monthly_done(session, row_id)
    with factory.begin() as session:
        row = svc.latest_completed(session)
        assert not svc.covers_holdings(session, uid, row)
        run.holding_unscored['LMT'] = 'no_sec_data'
        updated, changed = svc.store_run(session, run)
        assert changed and updated.id == row_id
        assert svc.covers_holdings(session, uid, updated)
        assert updated.metadata_json['monthly_notified_at']
    with factory.begin() as session:
        assert svc.result_of(svc.latest_completed(session)).holding_unscored == {'LMT': 'no_sec_data'}


async def test_pending_legacy_notifications_hide_ranks_and_strategy_labels(factory):
    uid = add_user(factory, 1)
    with factory.begin() as session:
        run = make_run()
        row, _ = svc.store_run(session, run)
        ref = svc._reference_signal(session, row, run, 'AAA')
        session.add(UserSignal(user_id=uid, signal_id=ref.id, action=svc.SignalAction.BUY,
                               recommendation='🟢 买入 AAA 约 $200\n    排名第 1 · 本月买入'))
    app = SimpleNamespace(bot=FakeBot(), bot_data={'session_factory': factory})
    assert await svc.deliver_pending(app) == (1, 0)
    text = app.bot.sent[0][1]
    assert '买入 AAA 约 $200' in text
    assert '排名第' not in text and '本月买入' not in text


def test_recommendations_never_include_ranks_reasons_or_candidate_list():
    data = {'status': 'ok', 'summary': {'equity': 100, 'cash': 100, 'top_candidates': [('AAA', 90)]},
            'signals': [{'action': 'BUY', 'symbol': 'AAA', 'kind': 'K1A_REPLACE', 'rank': 1, 'reason': 'secret', 'amount_usd': 100, 'est_shares': 1}], 'notes': []}
    text = render_user_signals(data)
    assert '买入 AAA' in text
    assert all(hidden not in text for hidden in ('排名', '换仓', '本月买入', 'secret', 'K1A'))

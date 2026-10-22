from types import SimpleNamespace

from quantbot import strategy_service as svc
from quantbot.models import UserSignal
from quantbot.strategy_text import check_pages, render_check, render_user_signals
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


def test_candidate_and_holding_scores_match_the_actual_user_scoring():
    run = make_run()
    held = {'AAA': 300, 'BBB': 100}
    scores = user_scores(run, held)
    data = {'status': 'no_candidates', 'summary': {'equity': 1000, 'cash': 600, 'buyable_symbols': []},
            'signals': [], 'notes': []}
    text = render_check(run, scores, held, data, 0)
    candidates, holdings = text.split('你的持仓排名')
    for symbol, score in scores.items():
        row = next(line for line in candidates.splitlines() if f'{symbol} ' in line)
        assert f"{score['total']:.1f} 分" in row
    for symbol in held:
        row = next(line for line in holdings.splitlines() if f'{symbol} ' in line)
        assert f"{scores[symbol]['total']:.1f} 分" in row
    assert '0–100' in text and '分数高不代表一定买入' in text


def test_breakdown_matches_native_component_scales_and_reversed_p_for_both_tables():
    run = make_run()
    scores = user_scores(run, {'AAA': 100})
    data = {'status': 'no_candidates', 'summary': {'equity': 100, 'cash': 0}, 'signals': [], 'notes': []}
    text = render_check(run, scores, {'AAA': 100}, data, 0)
    candidates, holdings = text.split('你的持仓排名')
    for section in [candidates, holdings]:
        lines = section.splitlines()
        index = next(i for i, line in enumerate(lines) if 'AAA ' in line)
        first, second = lines[index+1:index+3]
        assert first == '    P 20.0/30  V 20.0/25  F 10.0/15'
        assert second.removesuffix('</pre>') == f"    G 15.0/15  D {scores['AAA']['d_native']:.1f}/10  Q 5.0/5"
    assert '分项不直接相加' in text and 'P显示反向处理后的分值' in text
    # The renderer must not change any of the native scores used for selection.
    assert scores['AAA']['p_native'] == 10.0


def test_empty_portfolio_breakdown_shows_real_diversification_points():
    run = make_run()
    scores = user_scores(run, {})
    data = {'status': 'no_candidates', 'summary': {'equity': 100, 'cash': 100}, 'signals': [], 'notes': []}
    text = render_check(run, scores, {}, data, 0)
    assert scores['AAA']['d_native'] == 10
    assert 'D 10.0/10' in text


def test_unscored_holdings_do_not_receive_fabricated_zero_scores():
    run = make_run()
    scores = user_scores(run, {'AAA': 100})
    data = {'status': 'no_candidates', 'summary': {'equity': 200, 'cash': 0}, 'signals': [], 'notes': []}
    text = render_check(run, scores, {'AAA': 100, 'MISSING': 100}, data, 0)
    assert '暂无法评分的持仓（必要数据不足）：MISSING' in text
    assert not any('MISSING' in line and ' 分' in line for line in text.splitlines())


def test_long_scored_holdings_are_split_without_losing_rows_or_breaking_html():
    from tests.test_strategy_service import _assert_valid_telegram_html

    run = make_run()
    names = [f'STOCK{i:04d}' for i in range(200)]
    scores = {s: {'total': 90 - i / 10, 'gate_ok': True} for i, s in enumerate(names)}
    run.symbols = dict.fromkeys(names)
    held = dict.fromkeys(names, 100)
    data = {'status': 'no_candidates', 'summary': {'equity': 20000, 'cash': 0}, 'signals': [], 'notes': []}
    text = render_check(run, scores, held, data, 0)
    pages = check_pages(text)
    assert len(pages) > 1
    for page in pages:
        _assert_valid_telegram_html(page)
    joined = '\n'.join(pages)
    for i, symbol in enumerate(names):
        rows = [line for line in joined.splitlines() if f'{symbol} ' in line]
        assert len(rows) == (2 if i < 10 else 1)
        assert all(f"{scores[symbol]['total']:.1f} 分" in row for row in rows)
    # Each stock's two component lines stay on its page with the stock and total.
    for page in pages:
        lines = page.splitlines()
        for i, line in enumerate(lines):
            if 'STOCK' in line:
                assert lines[i+1].startswith('    P ')
                assert lines[i+2].startswith('    G ')


async def test_manual_check_delivers_all_score_pages(factory, accounts, monkeypatch):
    from quantbot import bot
    from tests.test_strategy_service import FakeApp, handler_args

    uid = add_user(factory, 1)
    with factory.begin() as session:
        svc.store_run(session, fresh_run())
    # Retain the real handler permissions/freshness path; isolate only its rendered content.
    text = '策略 1\n<pre>' + '\n'.join(f'STOCK{i:04d} 80.0 分' for i in range(400)) + '</pre>'
    monkeypatch.setattr(svc, 'strategy_settings', lambda: SETTINGS)
    monkeypatch.setattr(svc, 'check_text', lambda session, user_id, row, settings: text if user_id == uid else 'wrong user')
    app = FakeApp(factory, accounts)
    update, context, message = handler_args(factory, accounts, 1, app)
    await bot.check_one(update, context)
    assert message.texts == check_pages(text)
    assert len(message.texts) > 1 and not app.tasks


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

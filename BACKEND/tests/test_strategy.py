from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from quant_strategy import StrategyEngine, config, engine, parse_portfolio_csv, sec_fundamentals as sf, signals_for_user, strategy_info
from quant_strategy.signals import user_scores
from fixtures import make_bars, make_facts

AS_OF = date(2026, 10, 10)
NOW = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
SYMS = ['AAA', 'BBB', 'CCC', 'DDD', 'EEE', 'FFF']
SETTINGS = config.Settings(min_sale_usd=330.0, default_deposit_usd=200.0)


def test_parser_derives_q4_uses_symmetric_growth_and_is_point_in_time():
    m, why = sf.metrics(make_facts(), AS_OF, 40.0)
    assert why is None
    assert m['rev3'] == pytest.approx((220 / 100) ** (1 / 3) - 1, rel=0.02)
    assert m['margin'] == pytest.approx(0.2) and m['ey'] == pytest.approx(2.0 / 40.0)
    assert m['s_rev_now'] == pytest.approx(sf.sym_growth(80, 55)) and m['quarter_end'] == date(2026, 6, 30)
    early, why = sf.metrics(make_facts(), date(2026, 6, 1), 40.0)
    assert early is None and why == 'missing_year_ago_quarter'


def test_parser_handles_losses_and_foreign_filers_without_guessing():
    m, why = sf.metrics(make_facts(margin=-0.3, q_margin=-0.1), AS_OF, 40.0)
    assert why is None and m['margin'] == pytest.approx(-0.3) and np.isfinite(m['s_ni_now'])
    assert sf.metrics(dict(facts={'ifrs-full': {}}), AS_OF, 40.0) == (None, 'no_us_gaap_facts')
    assert sf.metrics(make_facts(), date(2028, 6, 1), 40.0) == (None, 'stale_annual_report')


def test_f_points_symmetric_is_bounded_and_monotone():
    assert 0 <= engine.f_points_sym(-1, -1, -1, -1) < engine.f_points_sym(1, 0, 1, 0) <= 15


def test_factor_betas_recover_a_known_beta():
    close, _ = engine.panels(make_bars(['AAA'], days=300, seed=3))
    close['XXX'] = 100 * np.cumprod(1 + (1.5 * close.pct_change()['SPY']).fillna(0))
    b = engine.factor_betas(close, ['XXX'])
    assert b['XXX'][0] == pytest.approx(1.5, abs=1e-6) and abs(b['XXX'][1]) < 1e-6


class FakeMarket:
    def __init__(self, bars):
        self.bars = bars

    def daily_bars(self, symbols, start, end=None, adjustment='all'):
        return self.bars[self.bars.symbol.isin(set(symbols))]


class FakeSec:
    def tickers(self):
        return {t: i + 1 for i, t in enumerate(SYMS)}, [dict(cik=i + 1, name=t, ticker=t) for i, t in enumerate(SYMS)]

    def companyfacts(self, cik):
        return make_facts(rev=(100, 130, 170, 200 + 15 * cik), eps=1 + 0.3 * cik)


POOL = dict(built='2026-09-01T00:00:00+00:00', leaders={'IYW': SYMS[:3], 'SOXX': SYMS[3:]}, skipped_duplicates={}, filings={}, leaders_per_group=5, extra_tech_etfs=True, dedupe_overlap=True)


def run_for(end='2026-10-09', extra=()):
    bars = make_bars(list(SYMS) + list(extra), end=pd.Timestamp(end, tz='UTC'))
    return StrategyEngine(SETTINGS, FakeMarket(bars), FakeSec(), load_pool=lambda: POOL, save_pool=lambda p: None, now=lambda: NOW).run(extra_symbols=extra)


def test_run_is_plain_data_with_components_and_no_user_state():
    run = run_for()
    assert run.status == 'ok' and run.as_of_bar == '2026-10-09' and set(run.symbols) == set(SYMS) and run.strategy == strategy_info()
    c = run.symbols['AAA']
    assert set(c) == {'price', 'sleeves', 'p_native', 'v_native', 'f_native', 'q_native', 'beta'} and 'd' not in c and 'total' not in c      # D is per user
    assert type(run).from_dict(run.to_dict()).symbols == run.symbols


def test_stale_and_failed_runs_produce_no_signals():
    stale = run_for(end='2026-08-01')
    assert stale.status == 'stale'
    us = signals_for_user(stale, parse_portfolio_csv('Company,Shares Held,Average Price\n'), SETTINGS, now=NOW)
    assert us.status == 'stale_data' and us.signals == []
    failed = StrategyEngine(SETTINGS, FakeMarket(pd.DataFrame(columns=['symbol', 'date', 'close', 'volume'])), FakeSec(), load_pool=lambda: POOL, save_pool=lambda p: None, now=lambda: NOW).run()
    assert failed.status == 'failed' and signals_for_user(failed, parse_portfolio_csv('Company,Shares Held,Average Price\n'), SETTINGS, now=NOW).status == 'run_failed'


def test_empty_portfolio_gets_a_monthly_buy_of_the_deposit():
    run = run_for()
    us = signals_for_user(run, parse_portfolio_csv('Company,Shares Held,Average Price\ncurrent_cash,0,200 USD\n'), SETTINGS, now=NOW)
    buys = [s for s in us.signals if s.kind == 'MONTHLY_BUY']
    assert us.status == 'ok' and buys and sum(s.amount_usd for s in buys) == pytest.approx(200.0) and all(s.action == 'BUY' for s in us.signals)
    assert us.disclaimer.startswith('Suggestion only')


def test_diversification_is_per_user_and_users_do_not_leak_into_each_other():
    run = run_for()
    a = user_scores(run, {'AAA': 5000.0})
    b = user_scores(run, {'FFF': 5000.0})
    empty = user_scores(run, {})
    assert a['AAA']['d'] != b['AAA']['d'] or a['BBB']['d'] != b['BBB']['d']
    scale = 70 / 55
    assert all(sc['d'] == pytest.approx(10.0 * scale) for sc in empty.values())      # empty portfolio: maximal diversification, scaled like the other components
    before = {s: dict(c) for s, c in run.symbols.items()}
    user_scores(run, {'AAA': 1.0})
    assert run.symbols == before                                    # the shared run is never mutated by a user's call


def test_signals_have_no_order_interface_and_use_only_the_given_portfolio():
    us = signals_for_user(run_for(), parse_portfolio_csv('Company,Shares Held,Average Price\ncurrent_cash,0,200 USD\n'), SETTINGS, now=NOW)
    assert set(us.to_dict()) == {'run_id', 'strategy', 'generated_at', 'status', 'signals', 'summary', 'notes', 'disclaimer'}
    assert not any(hasattr(us, n) for n in ('submit_order', 'place_order'))


def test_k1a_replaces_the_weakest_holding_when_cash_is_short():
    run = run_for()
    scores = user_scores(run, {})
    top = max(scores, key=lambda s: scores[s]['total'])
    others = [s for s in SYMS if s != top]
    csv = 'Company,Shares Held,Average Price\ncurrent_cash,0,5 USD\n' + ''.join(f'{s},100,{run.prices[s]:.2f} USD\n' for s in others)
    us = signals_for_user(run, parse_portfolio_csv(csv), SETTINGS, now=NOW)
    kinds = {s.kind for s in us.signals}
    assert kinds <= {'K1A_REPLACE', 'K5X_TRIM', 'MONTHLY_BUY'} and us.status in {'ok', 'no_candidates'}
    if 'K1A_REPLACE' in kinds:
        assert [s.action for s in us.signals] == ['SELL', 'BUY'] and us.signals[0].symbol in others


def test_versioning_is_stable_and_tracks_the_strategy_parameters(monkeypatch):
    v = strategy_info()['version']
    assert v == strategy_info()['version'] and len(v) == 12
    monkeypatch.setitem(config.STRATEGY, 'leaders_per_group', 3)
    assert strategy_info()['version'] != v


def test_text_messages_render_for_every_status_and_always_carry_the_disclaimer():
    from quant_strategy import messages
    run = run_for()
    empty = parse_portfolio_csv('Company,Shares Held,Average Price\ncurrent_cash,0,200 USD\n')
    text = messages.signals_text(signals_for_user(run, empty, SETTINGS, now=NOW))
    assert 'BUY' in text and 'Suggestion only' in text and 'P(rev)' in text
    assert 'AAA' in messages.rank_text(run) and 'IYW' in messages.pool_text(POOL) and run.run_id[:8] in messages.run_text(run)
    assert 'stale' in messages.signals_text(signals_for_user(run_for(end='2026-08-01'), empty, SETTINGS, now=NOW)).lower()


def test_no_cash_means_no_buy_amount_instead_of_a_zero_dollar_signal():
    us = signals_for_user(run_for(), parse_portfolio_csv('Company,Shares Held,Average Price\ncurrent_cash,0,0 USD\n'), SETTINGS, deposit_usd=0.0, now=NOW)
    assert us.signals == [] and us.status == 'no_candidates' and any('no amount to invest' in n for n in us.notes)


def test_monthly_frequency_and_zero_score_floor_are_the_configured_defaults_and_the_floor_works(monkeypatch):
    assert config.STRATEGY['decision_frequency'] == 'monthly' and config.STRATEGY['min_buy_score'] == 0
    assert strategy_info()['config']['min_buy_score'] == 0
    run = run_for()
    empty = parse_portfolio_csv('Company,Shares Held,Average Price\ncurrent_cash,0,200 USD\n')
    base = signals_for_user(run, empty, SETTINGS, now=NOW)
    top = max(s['total'] for s in user_scores(run, {}).values())
    assert base.signals
    monkeypatch.setitem(config.STRATEGY, 'min_buy_score', top + 1)
    high = signals_for_user(run, empty, SETTINGS, now=NOW)
    assert high.signals == [] and high.status == 'no_candidates'


def test_extra_holdings_are_scored_without_changing_buy_pool_or_other_users():
    class ExtraSec(FakeSec):
        def tickers(self):
            mapping, rows = super().tickers()
            return {**mapping, 'LMT': 20, 'OTHER': 21}, rows

    bars = make_bars(SYMS + ['LMT', 'OTHER'], end=pd.Timestamp('2026-10-09', tz='UTC'))
    engine_ = StrategyEngine(SETTINGS, FakeMarket(bars), ExtraSec(), load_pool=lambda: POOL, now=lambda: NOW)
    base = engine_.run()
    expanded = engine_.run(extra_symbols=['LMT', 'OTHER', 'MISSING'])
    assert expanded.status == 'ok'
    assert expanded.symbols == base.symbols and expanded.unscored == base.unscored and expanded.pool == base.pool
    assert set(expanded.holding_symbols) == {'LMT', 'OTHER'}
    assert 'MISSING' in expanded.holding_unscored
    assert set(user_scores(expanded, {'LMT': 1000})) == set(SYMS) | {'LMT'}
    assert user_scores(expanded, {}) == user_scores(base, {})
    empty = parse_portfolio_csv('Company,Shares Held,Average Price\ncurrent_cash,0,200 USD\n')
    before = signals_for_user(base, empty, SETTINGS, now=NOW)
    after = signals_for_user(expanded, empty, SETTINGS, now=NOW)
    assert after.summary == before.summary
    assert after.signals == before.signals
    restored = type(expanded).from_dict(expanded.to_dict())
    assert restored.holding_symbols == expanded.holding_symbols
    legacy = base.to_dict()
    legacy.pop('holding_symbols')
    legacy.pop('holding_unscored')
    assert type(base).from_dict(legacy).holding_symbols == {}


@pytest.mark.parametrize('cash,kind', [(0, 'K1A_REPLACE'), (5000, 'K5X_TRIM')])
def test_outside_holding_can_be_sold_but_never_bought(cash, kind):
    run = run_for()
    run.holding_symbols['LMT'] = dict(price=100.0, sleeves=[], p_native=30., v_native=0., f_native=0., q_native=0., beta=[1., 0., 0.])
    run.prices['LMT'] = 100.
    portfolio = parse_portfolio_csv(f'Company,Shares Held,Average Price\ncurrent_cash,0,{cash} USD\nLMT,100,100 USD\n')
    data = signals_for_user(run, portfolio, SETTINGS, now=NOW)
    assert [(s.kind, s.action) for s in data.signals] == [(kind, 'SELL'), (kind, 'BUY')]
    assert data.signals[0].symbol == 'LMT' and data.signals[1].symbol in run.symbols
    assert data.summary['pool'] == len(SYMS) and 'LMT' not in data.summary['buyable_symbols']
    # Even when the outside holding has the best score, it cannot become a buy candidate.
    run.holding_symbols['LMT'].update(p_native=0., v_native=25., f_native=15., q_native=5.)
    portfolio.holdings['LMT'].shares = 0.001
    portfolio.cash = 200
    data = signals_for_user(run, portfolio, SETTINGS, now=NOW)
    assert all(s.symbol != 'LMT' for s in data.signals if s.action == 'BUY')
    assert 'LMT' not in data.summary['buyable_symbols']

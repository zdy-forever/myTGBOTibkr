"""TQQQ100 + cash deployment profile, independent of archived GPIQ core tests."""
from datetime import date

import numpy as np
import pandas as pd
import pytest
from decimal import Decimal as D

from sgs.live import (
    MANAGED_TICKERS, TICKERS, VERSION, cfg, income_sleeve, recommendations,
    snapshot, target_weights, risk_scale, weights_text,
)


@pytest.mark.parametrize('bucket,expected', [
    ('SIDEWAYS', {}),
    ('DOWN', {}),
    ('STRONG_DOWN', {}),
    ('STRONG_UP', {'TQQQ': 1.}),
])
def test_adopted_targets_for_each_active_regime(bucket, expected):
    actual = target_weights(cfg(), bucket, {'ratio': 1.06}, 'SPMO', 1.)
    assert actual == pytest.approx(expected)


def test_strong_up_rejected_attack_stays_in_cash():
    actual = target_weights(cfg(), 'STRONG_UP', {'ratio': 1.06}, 'SPMO', .5,
                            level_scales=[.5, .5])
    assert actual == pytest.approx({'TQQQ': .5})
    assert not {'GPIQ', 'SCHD', 'SGOV', 'MQQQ', 'QQQP'} & actual.keys()
    assert income_sleeve({'GPIQ': .2, 'SCHD': .1, 'TQQQ': .7}) == pytest.approx({'SCHD': .3, 'TQQQ': .7})


@pytest.mark.parametrize('vol,expected', [(0., 1.), (.3, 1.), (1.2, .5), (3., .3)])
def test_attack_parameter_is_twenty_percent_with_tqqq_leverage(vol, expected):
    config = cfg()
    assert risk_scale(config, 'STRONG_UP', {'ratio': 1.06}, 'SPMO', vol, {'TQQQ': 1.}) == pytest.approx(expected)
    assert config['V5_VOL_TARGET_ANNUAL'] == .15  # Do not mutate other buckets.


def test_plain_up_is_unreachable_under_all_up_and_blocks_suggestions():
    assert cfg()['V5_STRONG_UP_RULE'] == 'ALL_UP'
    with pytest.raises(ValueError, match='趋势状态无效'):
        target_weights(cfg(), 'UP', {'ratio': 1.04}, 'SPMO', 1.)


class Market:
    def __init__(self, schd_volatility=0.):
        dates = pd.bdate_range('2014-01-01', '2026-10-19')
        i = np.arange(len(dates))
        amplitudes = dict(QQQ=0., QQQM=.015, SPMO=.015, TQQQ=.035,
                          SCHD=schd_volatility, SGOV=0.)
        self.rows = pd.concat([pd.DataFrame(dict(date=dates, symbol=t,
            close=100*np.exp(.001*i+amplitudes[t]*(-1.)**i))) for t in TICKERS])
        self.calls = []

    def daily_bars(self, symbols, start, end=None, adjustment='all'):
        self.calls.append((symbols, adjustment))
        return self.rows[self.rows.symbol.isin(symbols) & (self.rows.date >= pd.Timestamp(start)) &
                         (self.rows.date < pd.Timestamp(end).tz_localize(None))]

    def calendar(self, start, end):
        return [{'date': d.date().isoformat()} for d in pd.bdate_range(start, end)]


def test_snapshot_direct_schd_does_not_use_income_volatility_to_cascade():
    quiet, volatile = Market(), Market(.03)
    a = snapshot(quiet, date(2026, 10, 20))
    b = snapshot(volatile, date(2026, 10, 20))
    assert a['version'] == b['version'] == VERSION
    assert a['bucket'] == b['bucket'] == 'STRONG_UP'
    assert a['weights'] == b['weights']
    assert set(a['weights']) == {'TQQQ'}
    # Market fixture has high TQQQ volatility: rejected weight stays in cash.
    assert .2 < a['weights']['TQQQ'] < .8
    assert 'GPIQ' not in a['weights'] and 'GPIQ' not in b['prices']
    assert all('GPIQ' not in symbols for symbols, adj in volatile.calls if adj == 'all')
    assert set(volatile.calls[-1][0]) == set(MANAGED_TICKERS)


@pytest.mark.parametrize('old_version', ['strategy2-G2-20261020', 'strategy2-G2-SCHD20-20261021', 'strategy2-G2-SCHD20-direct-vol20-20261022', 'strategy2-G2-up15-side20-direct-vol20-tiered-20261024', 'strategy2-tqqq100-sgov-vol20-tiered-20261103'])
def test_old_same_date_snapshot_is_rewarmed_not_returned(old_version):
    actual = snapshot(Market(), date(2026, 10, 20),
                      {'version': old_version, 'as_of': '2026-10-19', 'state': {'obsolete': True}})
    assert actual['version'] == VERSION
    assert not {'GPIQ', 'SCHD', 'SGOV'} & actual['weights'].keys()


@pytest.mark.parametrize('nav', [400., 10000.])
def test_users_small_weight_changes_do_not_trigger_rebalancing(nav):
    invested = nav - cfg()['V5_CASH_RESERVE_USD']
    old = {'SCHD': .201, 'TQQQ': .729, 'SPMO': .035, 'QQQM': .035}
    data = {'weights': {'SCHD': .200, 'TQQQ': .757, 'SPMO': .021, 'QQQM': .021},
            'prices': dict.fromkeys(TICKERS, 100)}
    held = {t: w * invested / 100 for t, w in old.items()}
    assert recommendations(data, held, 13) == []


@pytest.mark.parametrize('deviation,should_trade', [(199.99, False), (200., True)])
def test_band_compares_actual_holdings_to_target_at_five_percent_of_nav(deviation, should_trade):
    data = {'weights': {'SCHD': .5, 'TQQQ': .5}, 'prices': dict.fromkeys(TICKERS, 100)}
    held = {'SCHD': (1993.5 + deviation) / 100, 'TQQQ': (1993.5 - deviation) / 100}
    assert bool(recommendations(data, held, 13)) == should_trade


def test_upgrade_exits_small_intermediate_positions_without_tiny_redeployment():
    data = {'weights': {'SCHD': .2, 'TQQQ': .8}, 'prices': dict.fromkeys(TICKERS, 100)}
    actions = recommendations(data, {'SCHD': .774, 'TQQQ': 2.896, 'SPMO': .1, 'QQQM': .1}, 13)
    assert {a['symbol'] for a in actions if a['action'] == 'SELL'} == {'SPMO', 'QQQM'}
    assert not any(a['action'] == 'BUY' for a in actions)


def test_legacy_gpiq_exit_funds_schd_and_never_buys_gpiq():
    data = {'weights': {'SCHD': 1.}, 'prices': dict.fromkeys(MANAGED_TICKERS, 100)}
    actions = recommendations(data, {'GPIQ': 10}, 0)
    assert actions[0]['symbol'] == 'GPIQ' and actions[0]['action'] == 'SELL'
    assert actions[0]['shares'] == 10
    assert actions[-1]['symbol'] == 'SCHD' and actions[-1]['action'] == 'BUY'
    cash = 0
    from sgs.live import commission
    for action in actions:
        quantity = action['shares']*(1 if action['action'] == 'BUY' else -1)
        price = 100*(1+np.sign(quantity)*cfg()['SLIPPAGE_PER_SIDE'])
        cash -= quantity*price+commission(quantity, price)
        assert cash >= 0
    assert cash >= cfg()['V5_CASH_RESERVE_USD']


def test_missing_legacy_quote_blocks_only_legacy_exposure():
    data = {'weights': {'SCHD': 1.}, 'prices': dict.fromkeys(TICKERS, 100)}
    assert recommendations(data, {}, 200)
    with pytest.raises(ValueError, match='GPIQ'):
        recommendations(data, {'GPIQ': 1}, 100)
    with pytest.raises(ValueError, match='无效'):
        recommendations(dict(data, weights={'GPIQ': 1}), {}, 100)


@pytest.mark.parametrize('scale', [.3, .5, 1.])
def test_defensive_target_is_all_cash_regardless_of_attack_scaling(scale):
    for bucket in ['SIDEWAYS', 'DOWN', 'STRONG_DOWN']:
        assert target_weights(cfg(), bucket, {'ratio': 1.}, 'SPMO', scale) == {}


@pytest.mark.parametrize('vol,tqqq', [(.3, 1.), (.7018, .6/.7018), (1.2, .5), (3., .3)])
def test_strong_up_tqqq_cap_is_one_hundred_percent(vol, tqqq):
    config = cfg()
    scale = risk_scale(config, 'STRONG_UP', {'ratio': 1.06}, 'SPMO', vol, {'TQQQ': 1.})
    actual = target_weights(config, 'STRONG_UP', {'ratio': 1.06}, 'SPMO', scale)
    assert actual['TQQQ'] == pytest.approx(tqqq)
    assert set(actual) == {'TQQQ'}


def test_existing_schd_is_fully_exited_into_new_targets():
    data = {'weights': {'TQQQ': .7}, 'prices': dict.fromkeys(MANAGED_TICKERS, 100)}
    actions = recommendations(data, {'TQQQ': 7, 'SCHD': D('.37'), 'SGOV': 2}, 13)
    assert dict(symbol='SCHD', action='SELL', shares=.37, reference_price=100.) in actions
    assert dict(symbol='SGOV', action='SELL', shares=2., reference_price=100.) in actions
    assert not any(a['action'] == 'BUY' for a in actions)


def test_defensive_regime_sells_everything_to_cash():
    data = {'weights': {}, 'prices': dict.fromkeys(MANAGED_TICKERS, 100)}
    actions = recommendations(data, {'TQQQ': D('.618405'), 'SCHD': D('.37')}, 13)
    assert sorted((a['symbol'], a['action'], a['shares']) for a in actions) == [
        ('SCHD', 'SELL', .37), ('TQQQ', 'SELL', .618405)]


@pytest.mark.parametrize('weights,text', [
    ({'TQQQ': .855}, 'TQQQ 85.5%、现金 14.5%'),
    ({'TQQQ': 1.}, 'TQQQ 100.0%'),
    ({}, '现金 100.0%'),
])
def test_weights_text_shows_cash_remainder(weights, text):
    assert weights_text(weights) == text

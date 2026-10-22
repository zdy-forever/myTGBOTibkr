"""The adopted deployment profile, independent of archived GPIQ core tests."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from sgs.live import (
    MANAGED_TICKERS, TICKERS, VERSION, cfg, income_sleeve, recommendations,
    snapshot, target_weights, risk_scale,
)


@pytest.mark.parametrize('bucket,expected', [
    ('SIDEWAYS', {'SCHD': 1.}),
    ('DOWN', {'SCHD': .2, 'SGOV': .8}),
    ('STRONG_DOWN', {'SCHD': .2, 'SGOV': .8}),
    ('STRONG_UP', {'SCHD': .2, 'TQQQ': .8}),
])
def test_adopted_targets_for_each_active_regime(bucket, expected):
    actual = target_weights(cfg(), bucket, {'ratio': 1.06}, 'SPMO', 1.)
    assert actual == pytest.approx(expected)


def test_strong_up_rejected_attack_goes_directly_to_schd():
    actual = target_weights(cfg(), 'STRONG_UP', {'ratio': 1.06}, 'SPMO', .5,
                            level_scales=[.5, .5])
    assert actual == pytest.approx({'SCHD': .6, 'TQQQ': .4})
    assert not {'GPIQ', 'MQQQ', 'QQQP'} & actual.keys()
    assert income_sleeve({'GPIQ': .2, 'SCHD': .1, 'TQQQ': .7}) == pytest.approx({'SCHD': .3, 'TQQQ': .7})


@pytest.mark.parametrize('vol,expected', [(0., 1.), (.3, 1.), (1.2, .5), (3., .3)])
def test_attack_parameter_is_twenty_percent_with_tqqq_leverage(vol, expected):
    config = cfg()
    assert risk_scale(config, 'STRONG_UP', {'ratio': 1.06}, 'SPMO', vol, {'TQQQ': 1.}) == pytest.approx(expected)
    assert config['V5_VOL_TARGET_ANNUAL'] == .15  # Do not mutate other buckets.


@pytest.mark.parametrize('bucket', ['UP', 'SIDEWAYS', 'DOWN', 'STRONG_DOWN'])
def test_other_regimes_preserve_native_sizing_and_weights(bucket):
    from sgs import regime_v5 as rv
    config, qsig = cfg(), {'ratio': 1.04}
    sleeve = rv.bucket_sleeve(config, bucket, 'SPMO', qsig)
    _, tilt = rv.up_bucket_tilt(config, qsig) if bucket == 'UP' else (None, None)
    expected = rv.cash_ratio_scale(config, config['CASH_RATIO_MODE'], bucket, qsig, tilt,
                                  .4, rv.bucket_risk_ticker(config, bucket, 'SPMO'), rv.sleeve_leverage(sleeve))
    actual = risk_scale(config, bucket, qsig, 'SPMO', .4, sleeve)
    assert actual == expected
    assert target_weights(config, bucket, qsig, 'SPMO', actual) == pytest.approx({
        t: w for t, w in income_sleeve(rv.effective_weights_v5(config, bucket, qsig, 'SPMO', expected)).items() if w > 1e-12})


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
    assert set(a['weights']) == {'TQQQ', 'SCHD'}
    # Market fixture has high TQQQ volatility: rejected weight goes to SCHD.
    assert .2 < a['weights']['SCHD'] < .8
    assert 'GPIQ' not in a['weights'] and 'GPIQ' not in b['prices']
    assert all('GPIQ' not in symbols for symbols, adj in volatile.calls if adj == 'all')
    assert set(volatile.calls[-1][0]) == set(MANAGED_TICKERS)


@pytest.mark.parametrize('old_version', ['strategy2-G2-20261020', 'strategy2-G2-SCHD20-20261021'])
def test_old_same_date_snapshot_is_rewarmed_not_returned(old_version):
    actual = snapshot(Market(), date(2026, 10, 20),
                      {'version': old_version, 'as_of': '2026-10-19', 'state': {'obsolete': True}})
    assert actual['version'] == VERSION
    assert 'GPIQ' not in actual['weights']


@pytest.mark.parametrize('nav', [400., 10000.])
def test_users_small_weight_changes_do_not_trigger_rebalancing(nav):
    invested = nav - cfg()['V5_CASH_RESERVE_USD']
    old = {'SCHD': .201, 'TQQQ': .729, 'SPMO': .035, 'QQQM': .035}
    data = {'weights': {'SCHD': .200, 'TQQQ': .757, 'SPMO': .021, 'QQQM': .021},
            'prices': dict.fromkeys(TICKERS, 100)}
    held = {t: w * invested / 100 for t, w in old.items()}
    assert recommendations(data, held, 13) == []


@pytest.mark.parametrize('deviation,should_trade', [(19.99, False), (20., True)])
def test_band_compares_actual_holdings_to_target_at_five_percent_of_nav(deviation, should_trade):
    data = {'weights': {'SCHD': .5, 'TQQQ': .5}, 'prices': dict.fromkeys(TICKERS, 100)}
    held = {'SCHD': (193.5 + deviation) / 100, 'TQQQ': (193.5 - deviation) / 100}
    assert bool(recommendations(data, held, 13)) == should_trade


def test_upgrade_exits_small_intermediate_positions_and_redeploys_sale_proceeds():
    data = {'weights': {'SCHD': .2, 'TQQQ': .8}, 'prices': dict.fromkeys(TICKERS, 100)}
    actions = recommendations(data, {'SCHD': .774, 'TQQQ': 2.896, 'SPMO': .1, 'QQQM': .1}, 13)
    assert {a['symbol'] for a in actions if a['action'] == 'SELL'} == {'SPMO', 'QQQM'}
    assert actions[-1]['symbol'] == 'TQQQ' and actions[-1]['action'] == 'BUY'
    assert actions[-1]['shares'] * 100 < 20


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
    assert recommendations(data, {}, 100)
    with pytest.raises(ValueError, match='GPIQ'):
        recommendations(data, {'GPIQ': 1}, 100)
    with pytest.raises(ValueError, match='无效'):
        recommendations(dict(data, weights={'GPIQ': 1}), {}, 100)

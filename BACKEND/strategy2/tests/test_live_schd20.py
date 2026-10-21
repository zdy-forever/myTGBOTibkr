"""The adopted deployment profile, independent of archived GPIQ core tests."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from sgs.live import (
    MANAGED_TICKERS, TICKERS, VERSION, cfg, income_sleeve, recommendations,
    snapshot, target_weights,
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


def test_cascade_keeps_original_sequence_and_replaces_every_income_allocation():
    actual = target_weights(cfg(), 'STRONG_UP', {'ratio': 1.06}, 'SPMO', .5,
                            level_scales=[.5, .5])
    assert actual == pytest.approx({'SCHD': .3, 'TQQQ': .4, 'SPMO': .1, 'QQQM': .1, 'SGOV': .1})
    assert not {'GPIQ', 'MQQQ', 'QQQP'} & actual.keys()
    assert income_sleeve({'GPIQ': .2, 'SCHD': .1, 'TQQQ': .7}) == pytest.approx({'SCHD': .3, 'TQQQ': .7})


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


def test_snapshot_uses_schd_volatility_and_never_needs_gpiq_signal_history():
    quiet, volatile = Market(), Market(.03)
    a = snapshot(quiet, date(2026, 10, 20))
    b = snapshot(volatile, date(2026, 10, 20))
    assert a['version'] == b['version'] == VERSION
    assert a['bucket'] == b['bucket'] == 'STRONG_UP'
    assert a['weights']['TQQQ'] == b['weights']['TQQQ']
    assert a['weights']['SPMO'] == b['weights']['SPMO']
    assert a['weights']['SCHD'] > b['weights']['SCHD'] >= .2
    assert b['weights']['SGOV'] > 0
    assert 'GPIQ' not in a['weights'] and 'GPIQ' not in b['prices']
    assert all('GPIQ' not in symbols for symbols, adj in volatile.calls if adj == 'all')
    assert set(volatile.calls[-1][0]) == set(MANAGED_TICKERS)


def test_old_same_date_snapshot_is_rewarmed_not_returned():
    actual = snapshot(Market(), date(2026, 10, 20),
                      {'version': 'strategy2-G2-20261020', 'as_of': '2026-10-19', 'state': {'obsolete': True}})
    assert actual['version'] == VERSION
    assert 'GPIQ' not in actual['weights']


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

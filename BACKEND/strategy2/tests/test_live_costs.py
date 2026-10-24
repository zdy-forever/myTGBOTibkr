"""Execution costs must not force overspending or prevent risk exits."""
from decimal import Decimal as D

import pytest

from sgs.live import TICKERS, _commission, recommendations


def data(price=66.42, weights=None, bucket='STRONG_UP'):
    return dict(prices=dict.fromkeys(TICKERS, price),
                weights=weights or {'TQQQ': .85, 'SCHD': .15}, bucket=bucket)


@pytest.mark.parametrize('quantity,price,expected', [
    ('0', '66.42', '0'), ('0.545054', '66.42', '0.4120248668'),
    ('1', '66.42', '.40'), ('2', '66.42', '.40'),
    ('200', '100', '.75'), ('1', '10', '.15'),
    ('1.5', '100', '.90'),
])
def test_tiered_budget_includes_buffer_and_fractional_exit_cost(quantity, price, expected):
    assert _commission(D(quantity), D(price)) == D(expected)


def test_reported_55_dollar_cash_does_not_trigger_tiny_tqqq_order():
    assert recommendations(data(), {}, 55) == []


def test_monthly_deposit_buys_whole_shares_without_buying_above_target():
    orders = recommendations(data(), {}, 200)
    assert orders == [dict(symbol='TQQQ', action='BUY', shares=2., reference_price=66.42)]
    assert 2 * D('66.42') <= D('187') * D('.85')


def test_lower_priced_etf_can_batch_multiple_shares_to_pass_floor():
    orders = recommendations(data(30, {'SCHD': 1}), {}, 110)
    assert orders == [dict(symbol='SCHD', action='BUY', shares=3., reference_price=30.)]


def test_old_execution_deduplication_cannot_suppress_new_cost_policy():
    held = {t: D(0) for t in TICKERS}
    previous = dict(execution_version='fractional-v4-up15-side20-direct-vol20',
                    weights={'SCHD': 1.}, holdings=held, cash=D(200))
    assert recommendations(data(100, {'SCHD': 1}), {}, 200, previous)


@pytest.mark.parametrize('price,cash,expected', [(79.99, 93.2, False), (80., 93.6, True)])
def test_absolute_floor_applies_after_quantity_rounding(price, cash, expected):
    assert bool(recommendations(data(price, {'SCHD': 1}), {}, cash)) == expected


def test_fee_ratio_is_checked_not_just_absolute_floor(monkeypatch):
    import sgs.live as live
    monkeypatch.setattr(live, 'EXTERNAL_FEE_BUFFER', D('.10'))
    assert recommendations(data(80, {'SCHD': 1}), {}, 100) == []


def test_buy_rounds_down_if_fee_and_slippage_make_second_share_unaffordable():
    orders = recommendations(data(100, {'SCHD': 1}), {}, D('213.5'))
    assert orders[0]['shares'] == 1


def test_small_ordinary_sale_waits_but_tqqq_risk_reduction_does_not():
    # NAV400, investible387; TQQQ target193.5 vs held220 ->26.5 sale.
    held = {'TQQQ': 2.2, 'SCHD': 1.67}
    orders = recommendations(data(100, {'TQQQ': .5, 'SCHD': .5}), held, 13)
    assert orders[0]['action'] == 'SELL' and orders[0]['symbol'] == 'TQQQ'
    assert orders[0]['shares'] == .265
    opposite = recommendations(data(100, {'TQQQ': .5, 'SCHD': .5}),
                               {'SCHD': 2.2, 'TQQQ': 1.67}, 13)
    assert opposite == []


@pytest.mark.parametrize('bucket', ['SIDEWAYS', 'DOWN', 'STRONG_DOWN'])
def test_defensive_regime_partial_equity_sale_is_cost_exempt(bucket):
    orders = recommendations(data(100, {'SCHD': .2, 'SGOV': .8}, bucket),
                             {'SCHD': 1, 'SGOV': 2.87}, 13)
    assert orders == [dict(symbol='SCHD', action='SELL', shares=.226, reference_price=100.)]


def test_full_fractional_exit_not_blocked_by_band_or_cost_gate():
    orders = recommendations(data(100, {'SCHD': 1}), {'TQQQ': D('.012345678')}, 13)
    assert orders[0]['shares'] == .012345
    assert orders[0]['action'] == 'SELL'


def test_dust_exit_never_spends_more_cash_than_available():
    assert recommendations(data(100, {'SCHD': 1}), {'TQQQ': D('.000001')}, 0) == []

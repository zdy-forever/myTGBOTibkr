"""Read-only G2 daily adapter. Uses closed bars; never imports the research runner."""
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
import math

import numpy as np
import pandas as pd

from .config_v5 import build_cfg_v5
from . import regime_v5 as rv

TICKERS = ('QQQ', 'QQQM', 'SPMO', 'GPIQ', 'TQQQ', 'SGOV')
LABELS = {'STRONG_UP': '强上涨', 'UP': '上涨', 'SIDEWAYS': '震荡',
          'DOWN': '下跌', 'STRONG_DOWN': '强下跌'}
VERSION = 'strategy2-G2-20261020'
EXECUTION_VERSION = 'fractional-v1'
SHARE_STEP = Decimal('0.000001')


def cfg():
    return build_cfg_v5('DEV_RECENT')


def _panel(bars, session_date):
    frame = bars.copy()
    frame['date'] = pd.to_datetime(frame['date'], utc=True).dt.tz_localize(None).dt.normalize()
    frame = frame[frame.date < pd.Timestamp(session_date)]
    return frame.pivot(index='date', columns='symbol', values='close').sort_index()


def snapshot(market, session_date, previous=None):
    """Persist classifier state after each closed session; replay missed sessions on restart."""
    config = cfg()
    start = (session_date - timedelta(days=800)).isoformat() if previous else '2014-01-01'
    end = session_date.isoformat() + 'T00:00:00Z'
    panel = _panel(market.daily_bars(TICKERS, start, end=end, adjustment='all'), session_date)
    calendar = market.calendar(session_date - timedelta(days=45), session_date - timedelta(days=1))
    expected = [pd.Timestamp(row['date']) for row in calendar]
    if not expected or not set(TICKERS) <= set(panel.columns):
        raise ValueError('S2 行情或交易日历不完整，暂停建议。')
    recent = panel.reindex(expected)[list(TICKERS)]
    if recent.isna().any().any() or not np.isfinite(recent.to_numpy()).all() or (recent <= 0).any().any():
        raise ValueError('S2 最近交易日行情缺失，暂停建议。')
    if panel.index[-1] != expected[-1]:
        raise ValueError('S2 行情尚未更新至上一交易日，暂停建议。')
    if previous and previous['as_of'] == expected[-1].date().isoformat():
        return previous
    state = rv.V5StreakState(**previous['state']) if previous else rv.V5StreakState()
    after = pd.Timestamp(previous['as_of']) if previous else pd.Timestamp('2015-12-31')
    if previous and after < panel.index[0]:
        raise ValueError('S2 状态与历史行情不连续，需要重新预热。')
    bucket = state.bucket
    qsig = ssig = slot = None
    for day in panel.index[panel.index > after]:
        history = panel.loc[:day]
        qqq = history.QQQ.dropna().to_numpy(dtype=float)
        if len(qqq) < 201:
            continue
        bucket, qsig = rv.step_v5(config, state, qqq)
        slot, ssig = rv.spmo_slot_signal(config, state, history.SPMO.dropna().to_numpy(dtype=float))
    # Repeated date is served from the durable snapshot by the host.
    if bucket is None or qsig is None:
        raise ValueError('S2 尚无新的完整交易日，暂停建议。')

    def vol(sleeve):
        if not sleeve:
            return None
        mixed = None
        for ticker, share in sleeve.items():
            prices = panel[ticker].tail(config['V5_VOL_LOOKBACK_DAYS']).to_numpy(dtype=float)
            if len(prices) != config['V5_VOL_LOOKBACK_DAYS'] or not np.isfinite(prices).all() or (prices <= 0).any():
                raise ValueError('S2 波动率预热数据不足，暂停建议。')
            part = share * np.diff(np.log(prices))
            mixed = part if mixed is None else mixed + part
        return float(np.std(mixed, ddof=1) * np.sqrt(252))

    sleeve = rv.bucket_sleeve(config, bucket, slot, qsig, ssig)
    _, tilt = rv.up_bucket_tilt(config, qsig) if bucket == 'UP' else (None, None)
    scale = rv.cash_ratio_scale(config, config['CASH_RATIO_MODE'], bucket, qsig, tilt,
                               vol(sleeve), rv.bucket_risk_ticker(config, bucket, slot), rv.sleeve_leverage(sleeve))
    levels = None
    if rv.derisk_asset(config, bucket) == 'CASCADE':
        levels = [rv.vol_target_scale(config, vol(level))
                  for level in rv.derisk_levels(config, bucket, slot, qsig, ssig)]
    weights = rv.effective_weights_v5(config, bucket, qsig, slot, scale, spmo_sig=ssig, level_scales=levels)
    raw = _panel(market.daily_bars(TICKERS, expected[-1].date().isoformat(), end=end, adjustment='raw'), session_date)
    if raw.empty or raw.index[-1] != expected[-1] or not set(TICKERS) <= set(raw.columns):
        raise ValueError('S2 未复权参考价格缺失，暂停建议。')
    prices = {ticker: float(raw.iloc[-1][ticker]) for ticker in TICKERS}
    if any(not math.isfinite(p) or p <= 0 for p in prices.values()):
        raise ValueError('S2 参考价格无效，暂停建议。')
    return dict(version=VERSION, as_of=expected[-1].date().isoformat(), state=asdict(state),
                bucket=bucket, trend=LABELS[bucket], weights=weights, prices=prices)


def _decimal(value):
    return Decimal(str(value))


def _commission(quantity, price):
    return min(max(abs(quantity) * Decimal('0.005'), Decimal(1)),
               abs(quantity) * price * Decimal('0.01'))


def commission(quantity, price):
    """Estimated cost model, not a broker fee quote."""
    return float(_commission(_decimal(quantity), _decimal(price)))


def _shares(value):
    return value.quantize(SHARE_STEP, rounding=ROUND_DOWN)


def _affordable_quantity(wanted, budget, fill_price):
    """Largest microshare quantity affordable including the piecewise fee model."""
    low = 0
    high = int(min(wanted, max(budget, Decimal(0)) / fill_price) / SHARE_STEP)
    while low < high:
        middle = (low + high + 1) // 2
        quantity = middle * SHARE_STEP
        if quantity * fill_price + _commission(quantity, fill_price) <= budget:
            low = middle
        else:
            high = middle - 1
    return low * SHARE_STEP


def recommendations(data, holdings, cash, previous=None):
    """Fractional-share, sell-before-buy suggestions bounded by reported cash.

    Only the strategy's ETF slice is managed. Other securities are left unchanged.
    A changed cash/holdings input also permits recomputation after a user records fills.
    Quantities round toward zero to six decimal places; never round a sale above
    the user's allocated holding or a purchase above the available cash.
    """
    config = cfg()
    prices = {t: _decimal(data['prices'][t]) for t in TICKERS}
    weights = {t: _decimal(w) for t, w in data['weights'].items()}
    held = {t: _decimal(holdings.get(t, 0)) for t in TICKERS}
    cash = _decimal(cash)
    if (not cash.is_finite() or cash < 0
            or any(not p.is_finite() or p <= 0 for p in prices.values())
            or any(not q.is_finite() or q < 0 for q in held.values())
            or any(not w.is_finite() or not 0 <= w <= 1 for w in weights.values())
            or sum(weights.values()) > Decimal('1.000001')):
        raise ValueError('S2 现金、持仓或行情数据无效，暂停建议。')
    nav = cash + sum(held[t] * prices[t] for t in held)
    if nav <= 0:
        return []
    signature = {t: round(w, 6) for t, w in weights.items()}
    if previous and previous.get('execution_version') == EXECUTION_VERSION and previous.get('weights') == signature and previous.get('holdings') == held and previous.get('cash') == cash:
        return []
    reserve = _decimal(config['V5_CASH_RESERVE_USD'])
    target = {t: _shares(weights.get(t, 0) * max(nav - reserve, 0) / prices[t]) for t in held}
    delta = {t: _shares(target[t] - held[t]) for t in held}
    available, freed, out = cash, Decimal(0), []
    slip = _decimal(config['SLIPPAGE_PER_SIDE'])
    for t in sorted(delta, key=lambda t: (delta[t], t)):
        quantity, price = delta[t], prices[t]
        if not quantity:
            continue
        closing = target[t] == 0 and held[t] > 0
        small = abs(quantity) * price < _decimal(config['V5_REBALANCE_BAND']) * nav
        if small and not closing:
            if quantity < 0 or not config['V5_REDEPLOY_FREED_CASH'] or freed <= 0:
                continue
            quantity = _affordable_quantity(quantity, freed, price * (1 + slip))
        if quantity > 0:
            quantity = _affordable_quantity(quantity, available - reserve, price * (1 + slip))
        if not quantity:
            continue
        fill = price * (1 + slip if quantity > 0 else 1 - slip)
        cost = quantity * fill + _commission(quantity, fill)
        available -= cost
        freed = freed - cost if quantity < 0 else max(0, freed - cost)
        out.append(dict(symbol=t, action='BUY' if quantity > 0 else 'SELL',
                        shares=float(abs(quantity)), reference_price=float(price)))
    return out

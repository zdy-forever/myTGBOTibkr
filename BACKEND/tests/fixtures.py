"""Synthetic SEC companyfacts and Alpaca-like data for tests (no network)."""
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd


def _row(start, end, val, filed, form):
    return dict(start=start, end=end, val=val, filed=filed, form=form)


def make_facts(rev=(100, 130, 170, 220), margin=0.2, cfo_ratio=0.25, eps=2.0, q_rev=(50, 55, 60, 70, 80), q_margin=0.2):
    """Annual FY2022-FY2025 (calendar years) plus quarters 2025-Q1..Q3 and 2026-Q1..Q2; the Q4 of 2025 must be derived by the parser."""
    years = [2022, 2023, 2024, 2025]
    annual = {'Revenues': [], 'NetIncomeLoss': [], 'NetCashProvidedByUsedInOperatingActivities': []}
    eps_rows = []
    for y, r in zip(years, rev):
        s, e, f = f'{y}-01-01', f'{y}-12-31', f'{y + 1}-02-20'
        annual['Revenues'].append(_row(s, e, r * 1e6, f, '10-K'))
        annual['NetIncomeLoss'].append(_row(s, e, r * margin * 1e6, f, '10-K'))
        annual['NetCashProvidedByUsedInOperatingActivities'].append(_row(s, e, r * cfo_ratio * 1e6, f, '10-K'))
        eps_rows.append(_row(s, e, eps, f, '10-K'))
    quarters = [('2025-01-01', '2025-03-31', '2025-05-05'), ('2025-04-01', '2025-06-30', '2025-08-05'), ('2025-07-01', '2025-09-30', '2025-11-05'),
                ('2026-01-01', '2026-03-31', '2026-05-05'), ('2026-04-01', '2026-06-30', '2026-08-05')]
    qrev, qni = [], []
    for (s, e, f), r in zip(quarters, q_rev):
        qrev.append(_row(s, e, r * 1e6, f, '10-Q'))
        qni.append(_row(s, e, r * q_margin * 1e6, f, '10-Q'))
    g = lambda rows: dict(units=dict(USD=rows))
    return dict(facts={'us-gaap': dict(Revenues=g(annual['Revenues'] + qrev), NetIncomeLoss=g(annual['NetIncomeLoss'] + qni),
                                       NetCashProvidedByUsedInOperatingActivities=g(annual['NetCashProvidedByUsedInOperatingActivities']),
                                       EarningsPerShareDiluted=dict(units={'USD/shares': eps_rows}))})


def make_bars(symbols, days=300, seed=0, drift=0.0005, vol=0.015, volume=2_000_000, end=None):
    """Long frame like AlpacaReadOnly.daily_bars; SPY/IWM/QQQ are included so betas can be estimated."""
    rng = np.random.default_rng(seed)
    end = end or pd.Timestamp(datetime.now(timezone.utc).date(), tz='UTC')
    dates = pd.bdate_range(end=end, periods=days, tz='UTC')
    market = rng.normal(drift, 0.01, days)
    rows = []
    for i, s in enumerate(sorted(set(symbols) | {'SPY', 'IWM', 'QQQ'})):
        beta = {'SPY': 1.0, 'IWM': 1.1, 'QQQ': 1.2}.get(s, 0.6 + 0.15 * (i % 7))
        r = beta * market + rng.normal(0, vol, days)
        close = 50 * (1 + i % 5) * np.cumprod(1 + r)
        rows += [dict(symbol=s, date=d, open=c, high=c, low=c, close=c, volume=volume) for d, c in zip(dates, close)]
    return pd.DataFrame(rows)

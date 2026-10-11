"""One shared strategy run = market-level analysis that does not depend on any user: ETF leaders pool, SEC point-in-time fundamentals, Alpaca prices.
It stores, per stock, the components P, V, F, Q and the three factor betas. The D (diversification) component depends on EACH USER's portfolio, so it is added per user
in signals.py: users never share portfolio data. The result is plain JSON-serialisable data (map it to `strategy_runs`)."""
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import config, logic, sec_fundamentals
from . import pool as poolmod
from .versioning import strategy_info


def f_points_sym(s_rev, s_rev_prev, s_ni, s_ni_prev):
    """F2_income from symmetric growth values S (revenue and net income): 65% growth + 35% acceleration, mean of the two, x15."""
    parts = [0.65 * logic.clip((now + 0.10) / 0.50) + 0.35 * logic.clip(((now - prev) + 0.15) / 0.30) for now, prev in ((s_rev, s_rev_prev), (s_ni, s_ni_prev))]
    return logic.NATIVE['F'] * float(np.mean(parts))


def panels(bars):
    close = bars.pivot(index='date', columns='symbol', values='close').sort_index()
    volume = bars.pivot(index='date', columns='symbol', values='volume').sort_index()
    return close, volume


def factor_betas(close, symbols, factors=config.FACTOR_ETFS):
    """OLS of daily returns on [1, SPY, IWM-SPY, QQQ-SPY] over the last 252 returns (>= 126 required): the three factor betas, as in the research."""
    if any(f not in close for f in factors):
        return {}
    ret = close.pct_change().dropna(how='all').tail(252)
    spy, iwm, qqq = (ret[f] for f in factors)
    out = {}
    for s in symbols:
        if s not in ret:
            continue
        joined = pd.concat([spy, iwm, qqq, ret[s]], axis=1, keys=['spy', 'iwm', 'qqq', 'y']).dropna()
        if len(joined) < 126:
            continue
        x = np.column_stack([np.ones(len(joined)), joined.spy, joined.iwm - joined.spy, joined.qqq - joined.spy])
        out[s] = np.linalg.lstsq(x, joined.y.values, rcond=None)[0][1:]
    return out


def average_dollar_volume(close, volume, symbol, multiplier=1.0):
    if symbol not in close:
        return float('nan')
    return float((close[symbol] * volume[symbol]).tail(20).mean() * multiplier)


def compute_components(pool, metrics, close, volume, settings, extra_symbols=()):
    """-> (symbols, unscored). symbols[s] = dict(price, sleeves, p_native, v_native, f_native, q_native, beta[3]); unscored[s] = reason. No D, no total: those are per user."""
    sleeves = poolmod.sleeves(pool)
    requested = set(sleeves) | set(extra_symbols)
    usable = {s: m for s, m in metrics.items() if s in requested and m is not None and s in close and close[s].dropna().size}
    unscored = {s: 'no_price_data' if metrics.get(s) is not None else 'no_sec_data' for s in requested if s not in usable}
    betas = factor_betas(close, list(usable))
    company = {s: logic.company_value(m['rev3'], m['margin'], m['cfo_ratio']) for s, m in usable.items()}
    out = {}
    for s, m in usable.items():
        # Holdings outside the pool use the fixed buy pool as their reference.
        # They never enter the reference samples of the original pool or of
        # other outside holdings, so one user's extra stock cannot alter peers.
        peers = [t for t in usable if t in sleeves and t != s and
                 (s not in sleeves or sleeves[s] & sleeves[t])]
        if len(peers) < 2:
            unscored[s] = 'fewer_than_2_peers'
            continue
        if s not in betas:
            unscored[s] = 'not_enough_price_history_for_beta'
            continue
        p = logic.p_points(company[s], [company[t] for t in peers])
        v = logic.v_points(m['ey'], [usable[t]['ey'] for t in peers])
        f = f_points_sym(m['s_rev_now'], m['s_rev_prev'], m['s_ni_now'], m['s_ni_prev'])
        q = logic.q_points(average_dollar_volume(close, volume, s, settings.dollar_volume_multiplier))
        if not all(np.isfinite(x) for x in (p, v, f, q)):
            unscored[s] = 'a_component_is_undefined'
            continue
        out[s] = dict(price=float(close[s].dropna().iloc[-1]), sleeves=sorted(sleeves.get(s, [])), p_native=float(p), v_native=float(v), f_native=float(f), q_native=float(q),
                      beta=[float(x) for x in betas[s]])
    return out, unscored


@dataclass
class RunResult:
    run_id: str
    started_at: str
    finished_at: str
    status: str                         # 'ok' | 'stale' | 'failed'
    as_of_bar: str                      # date of the latest price bar used ('' when failed)
    strategy: dict
    pool: dict
    symbols: dict
    unscored: dict
    prices: dict                        # last close of every symbol fetched (pool + the extra symbols passed in, e.g. users' holdings outside the pool)
    notes: list = field(default_factory=list)
    holding_symbols: dict = field(default_factory=dict)  # scored outside-pool holdings; never buy candidates
    holding_unscored: dict = field(default_factory=dict)

    @property
    def fresh(self):
        return self.status == 'ok'

    def to_dict(self):
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


class StrategyEngine:
    def __init__(self, settings, market, sec, load_pool=poolmod.load_pool, save_pool=poolmod.save_pool, now=None):
        """market: object with daily_bars(symbols, start) (AlpacaReadOnly); sec: SecClient. `now` is injectable for tests."""
        self.settings, self.market, self.sec = settings, market, sec
        self._load_pool, self._save_pool, self._now, self._ciks = load_pool, save_pool, now, None

    def now(self):
        return self._now() if self._now else datetime.now(timezone.utc)

    def ensure_pool(self, force=False):
        pool, notes = self._load_pool(), []
        if force or pool is None or poolmod.pool_age_days(pool, self.now()) > config.POOL_MAX_AGE_DAYS:
            try:
                _, rows = self.sec.tickers()
                pool = poolmod.build_pool(rows, today=self.now().date())
                self._save_pool(pool)
                notes.append('pool rebuilt from the latest N-PORT filings')
            except Exception as exc:
                if pool is None:
                    raise
                notes.append(f'pool refresh failed ({type(exc).__name__}); using the previous pool from {pool["built"][:10]}')
        return pool, notes

    def run(self, extra_symbols=(), refresh_pool=False) -> RunResult:
        started = self.now()
        info = strategy_info()
        base = dict(run_id=str(uuid.uuid4()), started_at=started.isoformat(timespec='seconds'), strategy=info)
        try:
            pool, notes = self.ensure_pool(force=refresh_pool)
            symbols = set(poolmod.sleeves(pool))
            extras = set(extra_symbols) - symbols
            bars = self.market.daily_bars(symbols | extras | set(config.FACTOR_ETFS), (started - timedelta(days=config.BAR_HISTORY_DAYS)).date())
            if bars.empty:
                raise RuntimeError('no price bars returned')
            close, volume = panels(bars)
            if self._ciks is None:
                self._ciks, _ = self.sec.tickers()
            metrics = {}
            for s in sorted(symbols | extras):
                try:
                    price = float(close[s].dropna().iloc[-1]) if s in close and close[s].notna().any() else None
                    m, why = (None, 'not_in_sec_ticker_file') if s not in self._ciks else sec_fundamentals.metrics(self.sec.companyfacts(self._ciks[s]), started.date(), price)
                    metrics[s] = m
                    if m is None:
                        notes.append(f'{s}: not scored ({why})')
                except Exception as exc:
                    metrics[s] = None
                    notes.append(f'{s}: SEC error ({type(exc).__name__})')
            comps, unscored = compute_components(pool, metrics, close, volume, self.settings, extras)
            holding_comps = {s: c for s, c in comps.items() if s in extras}
            holding_unscored = {s: why for s, why in unscored.items() if s in extras}
            comps = {s: c for s, c in comps.items() if s in symbols}
            unscored = {s: why for s, why in unscored.items() if s in symbols}
            last_bar = close.index.max().date()
            status = 'ok' if (started.date() - last_bar).days <= config.STALE_BAR_DAYS else 'stale'
            if status == 'stale':
                notes.append(f'latest price bar is {last_bar}: data is stale, no signals will be produced')
            prices = {s: float(close[s].dropna().iloc[-1]) for s in close.columns if close[s].notna().any()}
            return RunResult(**base, finished_at=self.now().isoformat(timespec='seconds'), status=status, as_of_bar=str(last_bar), pool=pool, symbols=comps, unscored=unscored, prices=prices, notes=notes,
                             holding_symbols=holding_comps, holding_unscored=holding_unscored)
        except Exception as exc:                                         # the host records a failed run instead of crashing
            return RunResult(**base, finished_at=self.now().isoformat(timespec='seconds'), status='failed', as_of_bar='', pool={}, symbols={}, unscored={}, prices={}, notes=[f'run failed: {type(exc).__name__}: {str(exc)[:200]}'])

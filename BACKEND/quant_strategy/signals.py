"""Per-user signals. A user's signals are computed ONLY from that user's own portfolio (passed in by the host bot) and the shared run: no data of any other user is read,
stored or returned. Nothing here places an order: signals are suggestions in plain data; the user decides.

Rules (R5 + K1A + K5x): score = reversed P3 (15 pts) + V/F/D/Q x70/55 + G 15; buyable only if reversed P >= 15 and the position is below its target weight
(max(1/pool size, 3.7%)); K1A replaces the weakest holding when a new top candidate cannot be paid from cash (gap >= 10 comparable points); K5x trims a position above 25%
when a candidate is >= 1.5 standard deviations better; otherwise the monthly amount goes to the top candidate (top two split by score when within 2 points)."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from . import config, logic

DISCLAIMER = 'Suggestion only: the system places no orders. Research result, not investment advice.'


@dataclass
class Signal:
    kind: str                 # 'K1A_REPLACE' | 'K5X_TRIM' | 'MONTHLY_BUY'
    action: str               # 'SELL' | 'BUY'
    symbol: str
    amount_usd: float
    est_shares: float
    reason: str
    score: float = None
    components: dict = field(default_factory=dict)
    sleeves: list = field(default_factory=list)


@dataclass
class UserSignals:
    run_id: str
    strategy: dict
    generated_at: str
    status: str               # 'ok' | 'no_candidates' | 'stale_data' | 'run_failed'
    signals: list
    summary: dict
    notes: list
    disclaimer: str = DISCLAIMER

    def to_dict(self):
        return dict(self.__dict__, signals=[s.__dict__ for s in self.signals])


def user_scores(run, held_values):
    """Totals for one user: shared components + this user's D (beta distance to the user's own portfolio, 10 when empty)."""
    components = {**run.symbols, **{s: c for s, c in run.holding_symbols.items() if s in held_values}}
    total = sum(v for v in held_values.values() if v > 0)
    parts = [v / total * np.array(components[s]['beta']) for s, v in held_values.items() if s in components and v > 0] if total > 0 else []
    pbeta = np.sum(parts, axis=0) if parts else None
    scores = {}
    for s, c in components.items():
        d = logic.d_points(c['beta'], pbeta)
        sc = logic.score(c['p_native'], c['v_native'], c['f_native'], d, c['q_native'])
        if sc is not None:
            scores[s] = dict(sc, d_native=float(d), p_native=c['p_native'], v_native=c['v_native'], f_native=c['f_native'], q_native=c['q_native'], price=c['price'], sleeves=c['sleeves'])
    return scores


def _held(run, portfolio, now):
    held, notes = {}, []
    for s, h in portfolio.holdings.items():
        if h.currency != 'USD':
            notes.append(f'{s}: amount in {h.currency} ignored (only USD is analysed)')
            continue
        price = run.prices.get(s)
        if price is None:
            price = h.avg_price
            notes.append(f'{s}: no market price in this run, valued at your average price')
        age = (now.date() - h.acquired_on).days if h.acquired_on else 0
        held[s] = dict(value=h.shares * price, cost=h.shares * h.avg_price, qty=h.shares, age_days=age)
    return held, notes


def signals_for_user(run, portfolio, settings, deposit_usd=None, blocked=None, now=None, protected=None) -> UserSignals:
    """blocked: optional {symbol: datetime until which it is not bought back} for names sold by a K1A / K5x suggestion (180 days).
    protected: optional {symbol: datetime until which K1A / K5x may not sell it} for names bought by such a suggestion (180 days, 365 if bought back)."""
    now = now or datetime.now(timezone.utc)
    head = dict(run_id=run.run_id, strategy=run.strategy, generated_at=now.isoformat(timespec='seconds'))
    if run.status == 'failed':
        return UserSignals(**head, status='run_failed', signals=[], summary={}, notes=list(run.notes))
    if run.status != 'ok':
        return UserSignals(**head, status='stale_data', signals=[], summary={}, notes=list(run.notes))
    held, notes = _held(run, portfolio, now)
    cash = portfolio.cash if portfolio.currency == 'USD' else 0.0
    if portfolio.currency != 'USD' and portfolio.cash:
        notes.append(f'cash in {portfolio.currency} ignored (only USD is analysed)')
    deposit = settings.default_deposit_usd if deposit_usd is None else deposit_usd
    scores = user_scores(run, {s: h['value'] for s, h in held.items()})
    equity = sum(h['value'] for h in held.values()) + cash
    pool_symbols = sorted(run.symbols) + sorted(run.unscored)
    target = logic.target_weight(len(pool_symbols), config.STRATEGY['target_weight_floor'])
    weights = {s: h['value'] / equity for s, h in held.items()} if equity > 0 else {}
    blocked, protected = blocked or {}, protected or {}
    ranked = [s for s in sorted(scores, key=lambda s: (-scores[s]['total'], s)) if s in run.symbols and scores[s]['gate_ok'] and scores[s]['total'] >= config.STRATEGY['min_buy_score'] and weights.get(s, 0.0) < target and not (s in blocked and now < blocked[s])]
    summary = dict(equity=equity, cash=cash, target_weight=target, deposit=deposit, top_candidates=[(s, round(scores[s]['total'], 2)) for s in ranked[:8]], buyable_symbols=ranked, scored=len(run.symbols), pool=len(pool_symbols))

    def sig(kind, action, sym, usd, reason):
        sc = scores.get(sym)
        px = (sc['price'] if sc else run.prices.get(sym)) or float('nan')
        comp = dict(p=sc['p_native'], v=sc['v_native'], f=sc['f_native'], d=sc['d_native'], q=sc['q_native'])      # native points; P is the raw value (lower is better) if sc else {}
        return Signal(kind, action, sym, round(usd, 2), round(usd / px, 4) if px == px else float('nan'), reason, round(sc['total'], 2) if sc else None, comp, sc['sleeves'] if sc else [])

    out = []
    k1a = logic.k1a_decide(logic.K1A(min_sale=settings.min_sale_usd), ranked, held, scores, cash, equity, protected, now)
    if k1a:
        sell, buy = k1a
        gap = scores[buy]['comparable'] - scores[sell]['comparable']
        out = [sig('K1A_REPLACE', 'SELL', sell, held[sell]['value'], f'whole position of your weakest holding; {buy} beats it by {gap:.1f} comparable points and cash is below the 5% target order'),
               sig('K1A_REPLACE', 'BUY', buy, held[sell]['value'] + cash, 'new top candidate, paid with the sale proceeds plus cash')]
    else:
        oa_spec = logic.OA(**{**logic.OA25X.__dict__, 'min_sale': settings.min_sale_usd})
        oa = logic.oa_decide(oa_spec, ranked, held, scores, equity, protected, now)
        if oa:
            sell, amount, buy = oa
            out = [sig('K5X_TRIM', 'SELL', sell, amount, f'{sell} is {weights.get(sell, 0):.0%} of your portfolio (above 25%) and {buy} is clearly better (>= 1.5 standard deviations); trim toward 20%'),
                   sig('K5X_TRIM', 'BUY', buy, amount + cash, 'reinvest the trim proceeds plus cash')]
        elif ranked and deposit > 0:
            out = [sig('MONTHLY_BUY', 'BUY', s, deposit * frac, 'top-ranked eligible name' + (' (top two split by score: within 2 points)' if len(logic.split_orders(ranked, scores)) == 2 else ''))
                   for s, frac in logic.split_orders(ranked, scores)]
        elif ranked:
            notes.append('no amount to invest: the monthly amount (or cash) is zero, so no buy amount can be suggested')
        else:
            notes.append('no buyable candidate: every scored name is gated out, at its target weight, or blocked')
    return UserSignals(**head, status='ok' if out else 'no_candidates', signals=out, summary=summary, notes=notes)

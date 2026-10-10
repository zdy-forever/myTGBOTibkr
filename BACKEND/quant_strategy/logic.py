import math
from dataclasses import dataclass

import numpy as np

LOGIC_VERSION = 5

# =====================================================================================================================
# STRATEGY LOGIC (pure Python + numpy; no QuantConnect objects, so it can be unit-tested locally)
# =====================================================================================================================
NATIVE = dict(P=30.0, V=25.0, F=15.0, D=10.0, Q=5.0)
G_POINTS = 15.0
P_WEIGHT = 15.0                 # points given to the reversed P term
SCALE = 70.0 / 55.0             # multiplier on V/F/D/Q native points
GATE_MIN_P = 15.0               # reversed-P gate (native points)
SPLIT_GAP = 2.0                 # top two are both bought when their total scores are within this many points


def clip(x, lo=0.0, hi=1.0):
    return float(min(max(x, lo), hi))


def midrank_pct(value, sample):
    """(count below + 0.5 * count equal) / n, sample INCLUDES the value itself (negative values take part)."""
    sample = list(sample)
    if not sample:
        return float('nan')
    below = sum(1 for s in sample if s < value)
    equal = sum(1 for s in sample if s == value)
    return (below + 0.5 * equal) / len(sample)


def as_fraction(x):
    """Morningstar ratios are fractions in most fields and percentages in a few; anything beyond +-5 (=500%) is read as a percentage."""
    if x is None or not math.isfinite(x):
        return float('nan')
    return x / 100.0 if abs(x) > 5.0 else float(x)


def sym_growth(g):
    """Symmetric growth S(a, b) = 2(a-b)/(|a|+|b|) from a plain growth rate g = a/b - 1 (b normalised to 1)."""
    if g is None or not math.isfinite(g):
        return float('nan')
    a = 1.0 + g
    return 2.0 * (a - 1.0) / (abs(a) + 1.0)


# ----------------------------------------------------------------------------------------------------------------- components
def company_value(rev_cagr_3y, net_margin, cfo_to_revenue):
    parts = [clip((rev_cagr_3y + 0.10) / 0.40), clip((net_margin + 0.10) / 0.40), clip(cfo_to_revenue / 0.40)]
    return float(np.mean(parts))


def p_points(company, peer_companies):
    """Native P (0-30) = 30 * percentile of Company among the peers and itself."""
    if len(peer_companies) < 2:
        return float('nan')
    return NATIVE['P'] * midrank_pct(company, list(peer_companies) + [company])


def v_points(earnings_yield, peer_yields):
    """V3: 25 * percentile of annual earnings yield among peers and itself."""
    if len(peer_yields) < 2 or not math.isfinite(earnings_yield):
        return float('nan')
    return NATIVE['V'] * midrank_pct(earnings_yield, list(peer_yields) + [earnings_yield])


def f_points(rev_growth_now, rev_growth_prev, ni_growth_now, ni_growth_prev):
    """F2_income: revenue and net income, 65% growth + 35% acceleration, mean of the two, x15. Growth inputs are plain growth rates."""
    vals = []
    for now, prev in ((rev_growth_now, rev_growth_prev), (ni_growth_now, ni_growth_prev)):
        s_now = sym_growth(now)
        s_prev = sym_growth(prev) if prev is not None else s_now  # no previous reading yet: acceleration 0 (flagged in README)
        if not math.isfinite(s_now) or not math.isfinite(s_prev):
            return float('nan')
        vals.append(0.65 * clip((s_now + 0.10) / 0.50) + 0.35 * clip(((s_now - s_prev) + 0.15) / 0.30))
    return NATIVE['F'] * float(np.mean(vals))


def d_points(beta_stock, beta_portfolio):
    """D4: 10*||b_s - b_p|| / (||b_s|| + ||b_p||); empty portfolio -> 10."""
    if beta_portfolio is None:
        return NATIVE['D']
    num = float(np.linalg.norm(np.asarray(beta_stock) - np.asarray(beta_portfolio)))
    den = float(np.linalg.norm(beta_stock) + np.linalg.norm(beta_portfolio))
    return NATIVE['D'] * num / den if den > 0 else float('nan')


def q_points(adv_usd):
    """Q (0-5). Research: liquidity 2/1, spread<=20bps 1, governance clear 1, data reliable 1. QuantConnect has no NBBO or governance feed, so
    spread, governance and data-reliability are taken as 1 point each for names with >= US$20M average dollar volume (documented approximation)."""
    if not math.isfinite(adv_usd):
        return float('nan')
    liquidity = 2 if adv_usd >= 100e6 else (1 if adv_usd >= 20e6 else 0)
    other = 3 if adv_usd >= 20e6 else 0
    return float(min(liquidity + other, 5))


# ----------------------------------------------------------------------------------------------------------------- score
def score(p_native, v, f, d, q):
    """Total (with G=15) and comparable (total - D). P is REVERSED: points = (30 - P) * P_WEIGHT / 30; gate on reversed native P."""
    if any(not math.isfinite(c) for c in (p_native, v, f, d, q)):
        return None
    reversed_p = NATIVE['P'] - p_native
    v_, f_, d_, q_ = v * SCALE, f * SCALE, d * SCALE, q * SCALE
    total = reversed_p * P_WEIGHT / NATIVE['P'] + v_ + f_ + d_ + q_ + G_POINTS
    return dict(total=total, comparable=total - d_, d=d_, gate_ok=reversed_p >= GATE_MIN_P)


# ----------------------------------------------------------------------------------------------------------------- policies
@dataclass(frozen=True)
class K1A:
    gap: float = 10.0
    target_weight: float = 0.05
    min_sale: float = 330.0      # ~A$500 expressed in US$; main.py scales it with the deposit size
    block_days: int = 180


@dataclass(frozen=True)
class OA:
    name: str
    trigger_weight: float
    trim_to: float
    gap_sigma: float = 1.5
    target_order_weight: float = 0.05
    min_sale: float = 330.0
    friction_cap: float = 0.15
    tax_rate: float = 0.325
    candidate_weight_factor: float = 0.5
    block_days: int = 180
    rebuy_protect_days: int = 365


OA25X = OA('OA25x', 0.25, 0.20)


def k1a_decide(spec: K1A, candidates, held, scores, cash, equity, protected, today):
    """candidates: buyable symbols, best first. held: {symbol: dict(value, ...)}. Returns (sell_symbol, buy_symbol) or None.
    New top candidate + cash below the target order -> sell the WHOLE weakest holding if the comparable-score gap >= spec.gap."""
    if not candidates or equity <= 0:
        return None
    cand = candidates[0]
    if cand in held:
        return None
    shortfall = spec.target_weight * equity - cash
    if shortfall < spec.min_sale:
        return None
    options = [(scores[s]['comparable'], s) for s, h in held.items() if s != cand and s in scores and h['value'] >= spec.min_sale and not (s in protected and today < protected[s])]
    if not options:
        return None
    weak_score, weak = min(options)
    if scores[cand]['comparable'] - weak_score < spec.gap:
        return None
    return weak, cand


def oa_decide(spec: OA, candidates, held, scores, equity, protected, today):
    """Opportunity-aware partial sale. held: {symbol: dict(value, cost, age_days)}. Returns (sell_symbol, sell_value, buy_symbol) or None.
    Concentration only STARTS an evaluation; the candidate must beat the holding by >= gap_sigma cross-sectional standard deviations."""
    if equity <= 0 or not candidates:
        return None
    comp = {s: v['comparable'] for s, v in scores.items()}
    sigma = float(np.std(list(comp.values())))
    weights = {s: h['value'] / equity for s, h in held.items()}
    over = [s for s, w in weights.items() if w > spec.trigger_weight and s in comp and not (s in protected and today < protected[s])]
    if not over or sigma <= 0:
        return None
    pool = [c for c in candidates if weights.get(c, 0.0) < spec.trigger_weight * spec.candidate_weight_factor and c not in over]
    for holding in sorted(over, key=lambda s: comp[s]):
        qualified = [c for c in pool if (comp[c] - comp[holding]) / sigma >= spec.gap_sigma]
        if not qualified:
            continue
        cand = qualified[0]
        value = held[holding]['value']
        amount = min((weights[holding] - spec.trim_to) * equity, spec.target_order_weight * equity, value)
        if amount < spec.min_sale:
            continue
        gain = max(amount - held[holding]['cost'] * amount / value, 0.0)
        discount = 0.5 if held[holding].get('age_days', 0) >= 365 else 1.0
        friction = (2 * 1.0 + amount * 2 * 5.0 / 10_000.0 + spec.tax_rate * gain * discount) / amount
        if friction > spec.friction_cap:
            continue
        return holding, float(amount), cand
    return None


def split_orders(ranked, scores, split_gap=SPLIT_GAP):
    """Top-1, or top-2 in proportion to their total scores when the gap is <= split_gap. Returns [(symbol, fraction)]."""
    if not ranked:
        return []
    if len(ranked) > 1 and scores[ranked[0]]['total'] - scores[ranked[1]]['total'] <= split_gap:
        a, b = scores[ranked[0]]['total'], scores[ranked[1]]['total']
        return [(ranked[0], a / (a + b)), (ranked[1], b / (a + b))]
    return [(ranked[0], 1.0)]


def target_weight(n_pool, floor=0.0):
    """Per-stock target weight: equal weight 1/N of the pool, but never below `floor`."""
    return max(1.0 / max(int(n_pool), 1), float(floor))


def assign_leaders(ranked, originals, extras, n=3, dedupe_extras=True):
    """ranked: {etf: [symbols by weight, best first]}. The ORIGINAL sleeves take their first n (never deduplicated, so results without extras are unchanged);
    each EXTRA sleeve then takes its first n symbols not already taken by an earlier sleeve. Returns ({etf: leaders}, {etf: [skipped duplicates]})."""
    leaders, skipped, taken = {}, {}, set()
    for etf in originals:
        leaders[etf] = list(ranked.get(etf, []))[:n]
        taken |= set(leaders[etf])
    for etf in extras:
        picks, skip = [], []
        for sym in ranked.get(etf, []):
            if dedupe_extras and sym in taken:
                skip.append(sym)
                continue
            picks.append(sym)
            if len(picks) == n:
                break
        leaders[etf], skipped[etf] = picks, skip
        taken |= set(picks)
    return leaders, skipped

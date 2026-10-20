"""V5: five trend-strength buckets (STRONG_UP/UP/SIDEWAYS/DOWN/STRONG_DOWN)
with continuous cash/equity and inter-ETF allocation, as a brand-new PARALLEL
experiment track alongside the original 6-regime state machine in
state_machine.py. This module is deliberately independent: it duplicates the
tiny `_confirm_v5` helper rather than importing state_machine's private
`_confirm`, and reuses only `state_machine.signals()` (a stable, pure,
StreakState-free utility) -- so nothing here can be affected by, or affect,
any future change to the original 6-regime track.

Design recap (see the approved plan for the full rationale):
- The 3-way SIDEWAYS/UP-family/DOWN-family split is driven ENTIRELY by QQQ's
  own trend (never SPMO). SPMO's own trend governs ONLY which ticker (QQQ or
  SPMO) occupies the UP bucket's risk slot -- it never changes the bucket
  itself.
- The 20% GPIQ floor is carved out FIRST, not clamped after the fact:
  effective_weights_v5() sets GPIQ's weight to V5_GPIQ_FLOOR as its very
  first step and only ever adds to it, so the floor invariant is true by
  construction, not by a renormalization pass.
- STRONG_UP/DOWN/STRONG_DOWN are full discrete risk-ticker swaps into a
  configurable asset (defaults TQQQ/PSQ/QID; see bucket_risk_ticker()); only
  UP mixes GPIQ and QQQ-or-SPMO continuously.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np

from . import state_machine as sm

V5_REGIMES = ("STRONG_UP", "UP", "SIDEWAYS", "DOWN", "STRONG_DOWN")
STRENGTH_CLASSIFIER_MODES = ("THRESHOLD_EXT", "MOMENTUM_VOL_PERCENTILE")
CASH_RATIO_MODES = ("VOL_TARGET", "SIGNAL_STRENGTH")

# Ticker leverage factors for VOL_TARGET mode's effective per-ticker target
# (cash_ratio_scale()'s risk_ticker param) -- TQQQ/QID's realized volatility
# is structurally ~3x/~2x QQQ's by design (that's what "3x leveraged" means),
# so scaling the effective target by the fund's own published leverage factor
# is more principled than independently fitting a per-ticker target. PSQ
# (-1x) gets no adjustment: realized vol via np.std is magnitude-only, so an
# inverse (non-leveraged) ETF's vol is already comparable to QQQ's own.
# Lives here (not config_v5.py) to avoid a circular import -- config_v5
# already imports CASH_RATIO_MODES/STRENGTH_CLASSIFIER_MODES from this
# module, and this is also where the rest of the bucket-to-ticker mapping
# conceptually lives.
V5_TICKER_LEVERAGE_MULTIPLIER = {
    "TQQQ": 3.0, "QLD": 2.0, "QID": 2.0, "PSQ": 1.0, "QQQ": 1.0, "QQQM": 1.0,
    "SPMO": 1.0, "GPIQ": 1.0, "BIL": 1.0, "SGOV": 1.0,
}

# What each discretely-swapped bucket holds in its risk slot. "SPMO_SLOT"
# resolves to the UP bucket's own SPMO-trend-gated pick (spmo_slot_signal);
# "CASH" holds nothing (the slice stays idle cash). Defaults (TQQQ/PSQ/QID)
# are the original design; the alternatives exist because measuring each
# candidate on the days V5 actually assigned to these buckets showed the
# leveraged/inverse defaults lost money on most of them (see config_v5.py).
# "TQQQ_GRADED" holds TQQQ and QQQM in proportion to how many of the four
# textbook strength signals agree (see strength_votes): k of 4 -> TQQQ k/4,
# QQQM the rest, so leverage steps 1x, 1.5x, 2x, 2.5x, 3x with conviction.
# "TQQQ_QLD" holds TQQQ and QLD half each (2.5x).
STRONG_UP_ASSETS = ("TQQQ", "QLD", "QQQM", "SPMO", "SPMO_SLOT", "SPMO_QQQM_EQUAL",
                    "SPMO_QQQM_TREND", "TQQQ_GRADED", "TQQQ_QLD")

# What the UP / CHOPPY_UP risk slot holds:
#   SPMO_GATED  -- original: SPMO while SPMO's own trend qualifies, else QQQM
#   EQUAL       -- SPMO and QQQM 50/50, always both
#   TREND       -- SPMO and QQQM both, split by their relative trend strength
#                  (each one's ratio to its own MA200; see sleeve_split())
UP_SLOT_MODES = ("SPMO_GATED", "EQUAL", "TREND")

# How STRONG_UP is decided inside a confirmed uptrend:
#   THRESHOLD   -- original: per STRENGTH_CLASSIFIER_MODE (ratio/ret63
#                  thresholds or momentum percentile)
#   EFFICIENCY  -- the trend's last V5_STRONG_ER_LOOKBACK days moved up in a
#                  nearly straight line (efficiency ratio >= V5_STRONG_ER_MIN
#                  and a positive net move). On the days the default
#                  classifier called up-family, this was the only candidate
#                  definition with a strong edge in BOTH dev periods.
#   Rules built only from textbook trend-following windows (20/50/200-day
#   averages, 1/3/6/12-month returns, the 52-week high, 20-day vol vs its own
#   1-year median) -- no fitted thresholds, so nothing to overfit:
#   MA_STACK    -- close > MA20 > MA50 > MA200, and MA50 and MA200 both rising
#                  over the last 20 days (every trend layer aligned)
#   MOM_ALL     -- the 1-, 3-, 6- and 12-month returns are all positive
#   NEAR_HIGH   -- close within 5% of its 252-day high
#   CALM_TREND  -- close above MA50 and MA200, and 20-day realized vol below
#                  its own 252-day median (a quiet uptrend: where daily-reset
#                  leverage decays least)
#   MOM_CALM    -- MOM_ALL and CALM_TREND together
#   ALL_UP      -- every confirmed up-family day is STRONG_UP (no separate
#                  strength test at all)
#   VOTE_2 / VOTE_3 -- at least 2 / 3 of MA_STACK, MOM_ALL, NEAR_HIGH,
#                  CALM_TREND agree (see strength_votes)
STRONG_UP_RULES = ("THRESHOLD", "EFFICIENCY", "MA_STACK", "MOM_ALL", "NEAR_HIGH", "CALM_TREND", "MOM_CALM",
                   "ALL_UP", "VOTE_2", "VOTE_3")
VOTE_SIGNALS = ("MA_STACK", "MOM_ALL", "NEAR_HIGH", "CALM_TREND")

# Which price series the EFFICIENCY strong-up rule reads:
#   NDX         -- the Nasdaq-100 signal (QQQ history stitched to QQQM)
#   TQQQ        -- TQQQ's own adjusted history (efficiency ratio + rising)
#   TQQQ_MA200  -- as TQQQ, and TQQQ must also be above its own MA200
STRONG_SIGNAL_SOURCES = ("NDX", "TQQQ", "TQQQ_MA200")

# Where the slice trimmed by the cash/equity scaling (1 - invest_scale) goes
# in UP / STRONG_UP: CASH (left idle, the original), SGOV (T-bills), GPIQ, or
# NDX (V5's Nasdaq-100 fund, QQQM) -- for STRONG_UP the last one de-levers
# TQQQ into QQQM instead of into cash. CASCADE steps down one level at a time
# (see derisk_levels): STRONG_UP's trimmed slice is offered to UP's SPMO/QQQM
# sleeve, then to GPIQ; UP's goes to GPIQ. Each level takes the share its own
# volatility target allows (vol_target_scale) and passes the rest down;
# whatever is left at the bottom goes to SGOV.
DERISK_ASSETS = ("CASH", "SGOV", "GPIQ", "NDX", "CASCADE")
DOWN_ASSETS = ("PSQ", "BIL", "SGOV", "CASH", "GPIQ")
STRONG_DOWN_ASSETS = ("QID", "PSQ", "BIL", "SGOV", "CASH", "GPIQ")

# How many trend levels the classifier distinguishes. FIVE is the original
# STRONG_UP/UP/SIDEWAYS/DOWN/STRONG_DOWN. The two seven-level modes add
# CHOPPY_UP (震荡上涨) and CHOPPY_DOWN (震荡下跌) between SIDEWAYS and UP/DOWN,
# each holding a continuous blend of its two neighbours:
#   RATIO_BAND -- splits the old SIDEWAYS band by QQQ's ratio to its MA200
#                 (ratio in [V5_CHOPPY_UP_RATIO, V5_UP_RATIO) -> CHOPPY_UP,
#                 ratio in (V5_DOWN_RATIO, V5_CHOPPY_DOWN_RATIO] -> CHOPPY_DOWN);
#   EFFICIENCY -- keeps the 5-bucket trend calls but demotes a confirmed UP/DOWN
#                 to CHOPPY_UP/CHOPPY_DOWN when QQQ's 20-day efficiency ratio
#                 (net move / total path) is below V5_CHOPPY_ER_MAX, i.e. the
#                 trend is there but the path is noisy.
BUCKET_MODES = ("FIVE", "RATIO_BAND", "EFFICIENCY")
V5_REGIMES_7 = ("STRONG_UP", "UP", "CHOPPY_UP", "SIDEWAYS", "CHOPPY_DOWN", "DOWN", "STRONG_DOWN")


def bucket_risk_ticker(cfg_v5, bucket, spmo_slot):
    """The ticker occupying `bucket`'s risk slot, or None when there is no
    risk slot (SIDEWAYS) or the configured asset is CASH. Single source of
    truth shared by effective_weights_v5() and the engine's realized-vol
    lookup, so the two can never disagree about what is actually held."""
    if bucket in ("UP", "CHOPPY_UP"):
        return spmo_slot
    if bucket == "SIDEWAYS":
        return None
    asset = cfg_v5[{"STRONG_UP": "V5_STRONG_UP_ASSET", "DOWN": "V5_DOWN_ASSET",
                    "CHOPPY_DOWN": "V5_DOWN_ASSET",
                    "STRONG_DOWN": "V5_STRONG_DOWN_ASSET"}[bucket]]
    if asset == "SPMO_SLOT":
        return spmo_slot
    if asset == "CASH":
        return None
    if asset in ("SPMO_QQQM_EQUAL", "SPMO_QQQM_TREND"):
        return "SPMO"  # multi-ticker sleeve; see bucket_sleeve() for the full split
    if asset in ("TQQQ_GRADED", "TQQQ_QLD"):
        return "TQQQ"  # mixed sleeve; see bucket_sleeve() and sleeve_leverage()
    return asset


def sleeve_split(cfg_v5, mode, spmo_slot, ndx_sig, spmo_sig):
    """{ticker: share} for an SPMO/QQQM risk sleeve (shares sum to 1).
    SPMO_GATED -> the single gated pick; EQUAL -> 50/50; TREND -> each gets a
    share proportional to how far its price sits above its own MA200
    (max(ratio - 1, 0)); if neither is above its MA200 it falls back to 50/50,
    and if a signal is missing the split falls back to the gated pick."""
    ndx = cfg_v5["V5_NDX_TICKER"]
    if mode == "SPMO_GATED":
        return {spmo_slot: 1.0}
    if mode == "EQUAL":
        return {"SPMO": 0.5, ndx: 0.5}
    if mode == "TREND":
        if ndx_sig is None or spmo_sig is None:
            return {spmo_slot: 1.0}
        a = max(spmo_sig["ratio"] - 1.0, 0.0)
        b = max(ndx_sig["ratio"] - 1.0, 0.0)
        if a + b <= 0:
            return {"SPMO": 0.5, ndx: 0.5}
        return {"SPMO": a / (a + b), ndx: b / (a + b)}
    raise AssertionError(f"unreachable sleeve mode {mode!r}")


def bucket_sleeve(cfg_v5, bucket, spmo_slot, ndx_sig=None, spmo_sig=None):
    """{ticker: share} for the bucket's whole risk slot (empty for SIDEWAYS or
    a CASH asset). The single source of truth for what the slot holds, used
    both to build target weights and to measure the slot's volatility."""
    if bucket in ("UP", "CHOPPY_UP"):
        return sleeve_split(cfg_v5, cfg_v5["V5_UP_SLOT_MODE"], spmo_slot, ndx_sig, spmo_sig)
    if bucket == "STRONG_UP":
        asset = cfg_v5["V5_STRONG_UP_ASSET"]
        if asset == "SPMO_QQQM_EQUAL":
            return sleeve_split(cfg_v5, "EQUAL", spmo_slot, ndx_sig, spmo_sig)
        if asset == "SPMO_QQQM_TREND":
            return sleeve_split(cfg_v5, "TREND", spmo_slot, ndx_sig, spmo_sig)
        if asset == "TQQQ_QLD":
            return {"TQQQ": 0.5, "QLD": 0.5}
        if asset == "TQQQ_GRADED":
            share = (ndx_sig or {}).get("strength_votes", 0) / len(VOTE_SIGNALS)
            sleeve = {"TQQQ": share, cfg_v5["V5_NDX_TICKER"]: 1.0 - share}
            return {t: w for t, w in sleeve.items() if w > 0}
    ticker = bucket_risk_ticker(cfg_v5, bucket, spmo_slot)
    return {} if ticker is None else {ticker: 1.0}


def sleeve_leverage(sleeve):
    """Share-weighted leverage of a {ticker: share} sleeve (1.0 if empty):
    the multiple the volatility target scales by. Equals the single ticker's
    own multiplier for every one-ticker or all-1x sleeve."""
    if not sleeve:
        return 1.0
    return sum(share * V5_TICKER_LEVERAGE_MULTIPLIER.get(t, 1.0) for t, share in sleeve.items()) / sum(sleeve.values())

def derisk_asset(cfg_v5, bucket):
    """The configured home for `bucket`'s trimmed slice (DERISK_ASSETS), or
    None for buckets that don't park it anywhere."""
    if bucket == "STRONG_UP":
        return cfg_v5.get("V5_STRONG_DERISK_ASSET", "CASH")
    if bucket in ("UP", "CHOPPY_UP"):
        return cfg_v5.get("V5_UP_DERISK_ASSET", "CASH")
    return None


def derisk_levels(cfg_v5, bucket, spmo_slot, ndx_sig=None, spmo_sig=None):
    """The lower levels a CASCADE trimmed slice steps through, in order, each
    as a {ticker: share} sleeve: STRONG_UP -> UP's risk sleeve, then GPIQ;
    UP / CHOPPY_UP -> GPIQ. Empty for every other bucket."""
    if bucket == "STRONG_UP":
        return [bucket_sleeve(cfg_v5, "UP", spmo_slot, ndx_sig, spmo_sig), {"GPIQ": 1.0}]
    if bucket in ("UP", "CHOPPY_UP"):
        return [{"GPIQ": 1.0}]
    return []


V5_STREAK_LABELS = ("up", "down", "strong_up", "strong_down", "spmo_bull", "spmo_exit")


@dataclass
class V5StreakState:
    bucket: str | None = None
    signal_streak: dict = field(default_factory=lambda: {k: 0 for k in V5_STREAK_LABELS})
    spmo_slot: str | None = None  # "QQQ" or "SPMO" -- UP bucket's current risk-slot holding
    strong_hold_days: int = 0     # consecutive days already spent in STRONG_UP (EFFICIENCY rule)
    up_age: int = 0               # consecutive days the up-family has been confirmed


def _confirm_v5(state, label, condition, confirm_days):
    """Independent duplicate of state_machine._confirm() -- deliberately not
    imported (that's a private helper scoped to the old track's StreakState),
    so V5 has zero runtime coupling to state_machine.py beyond signals()."""
    state.signal_streak[label] = state.signal_streak[label] + 1 if condition else 0
    return state.signal_streak[label] >= confirm_days


def _momentum_percentile(cfg_v5, prices):
    """Today's ret63 percentile rank within its own trailing
    V5_PERCENTILE_LOOKBACK-day history of ret63 values (plain numpy rank
    arithmetic, 0-100 scale). Returns None if there isn't enough history for
    a full lookback window of ret63 values PLUS the 201-point floor each
    individual ret63 computation itself needs (same conservative-failure
    shape as state_machine.signals())."""
    lookback = cfg_v5["V5_PERCENTILE_LOOKBACK"]
    needed = lookback + 201
    if len(prices) < needed:
        return None
    p = np.asarray(prices, dtype=float)
    history = []
    for i in range(len(p) - lookback, len(p) + 1):
        window = p[:i]
        ret63 = window[-1] / window[-64] - 1.0
        history.append(ret63)
    today = history[-1]
    past = np.asarray(history[:-1], dtype=float)
    percentile = float(np.mean(past <= today) * 100.0)
    return percentile


def step_v5(cfg_v5, state, qqq_prices, strong_prices=None):
    """Advances the 5-bucket classifier by one trading day. Returns
    (bucket, qqq_sig) or (None, None) if QQQ's own history isn't long enough
    yet (mirrors state_machine.step()'s early-return shape). Mutates `state`
    in place (streak counters, bucket).

    V5_CONFIRM_DAYS (up/strong_up) and V5_DOWN_CONFIRM_DAYS (down/strong_down)
    are intentionally separate -- an earlier round raised V5_CONFIRM_DAYS
    (then shared by all four) from 2 to 4 to fix a real UP<->STRONG_UP
    whipsaw, but that same change had an unintended side effect on the
    DOWN side: during the real 2018-10/11 QQQ correction, ratio oscillated
    around V5_DOWN_RATIO repeatedly, and confirm_days=4 delayed DOWN
    confirmation from 2018-10-31 to 2018-11-23 (23 extra days nearly fully
    invested in GPIQ while QQQ fell -6.25%). Splitting the two restores
    fast downside confirmation without reintroducing the upside whipsaw
    (they are independent streak labels -- see test coverage)."""
    qqq_sig = sm.signals(cfg_v5, qqq_prices)
    if qqq_sig is None:
        return None, None
    ratio, ret63 = qqq_sig["ratio"], qqq_sig["ret63"]
    confirm_days = cfg_v5["V5_CONFIRM_DAYS"]
    down_confirm_days = cfg_v5["V5_DOWN_CONFIRM_DAYS"]

    up_family = _confirm_v5(state, "up", ratio >= cfg_v5["V5_UP_RATIO"], confirm_days)
    # Optional exit band: once in the up-family, stay until the ratio falls
    # back below V5_UP_EXIT_RATIO (None = exit as soon as the entry condition
    # fails, the original behaviour). Keeps small dips from flipping a
    # confirmed uptrend out and back in.
    exit_ratio = cfg_v5.get("V5_UP_EXIT_RATIO")
    if exit_ratio is not None and state.bucket in ("UP", "STRONG_UP") and ratio >= exit_ratio:
        up_family = True
    down_family = _confirm_v5(state, "down", ratio <= cfg_v5["V5_DOWN_RATIO"], down_confirm_days)

    mode = cfg_v5["STRENGTH_CLASSIFIER_MODE"]
    if mode == "THRESHOLD_EXT":
        strong_up = _confirm_v5(state, "strong_up", (
            ratio >= cfg_v5["V5_STRONG_UP_RATIO"] and ret63 >= cfg_v5["V5_STRONG_UP_RET63"]
        ), confirm_days)
        strong_down = _confirm_v5(state, "strong_down", (
            ratio <= cfg_v5["V5_STRONG_DOWN_RATIO"] and ret63 <= cfg_v5["V5_STRONG_DOWN_RET63"]
        ), down_confirm_days)
    elif mode == "MOMENTUM_VOL_PERCENTILE":
        percentile = _momentum_percentile(cfg_v5, qqq_prices)
        if percentile is None:
            strong_up = _confirm_v5(state, "strong_up", False, confirm_days)
            strong_down = _confirm_v5(state, "strong_down", False, down_confirm_days)
        else:
            strong_up = _confirm_v5(state, "strong_up", percentile >= cfg_v5["V5_STRONG_PERCENTILE"], confirm_days)
            strong_down = _confirm_v5(state, "strong_down", percentile <= cfg_v5["V5_WEAK_PERCENTILE"], down_confirm_days)
    else:
        raise AssertionError(f"unreachable STRENGTH_CLASSIFIER_MODE {mode!r}")

    # Optional alternative STRONG_UP rule. It only replaces the strong_up
    # decision; the classifier's own strong_up streak above still advances
    # every day, so switching rules never desynchronises other state.
    if cfg_v5.get("V5_STRONG_UP_RULE", "THRESHOLD") == "EFFICIENCY":
        lookback = cfg_v5["V5_STRONG_ER_LOOKBACK"]
        # Which price series decides strong-up: the Nasdaq-100 signal itself
        # (default) or the strong-up asset's own history (strong_prices,
        # passed by the engine when V5_STRONG_SIGNAL_SOURCE != "NDX"). The
        # up-family gate above always stays on the Nasdaq-100 signal.
        src = qqq_prices if strong_prices is None else strong_prices
        er = efficiency_ratio(src, lookback)
        rising = len(src) > lookback and src[-1] > src[-(lookback + 1)]
        if cfg_v5.get("V5_STRONG_SIGNAL_SOURCE", "NDX") == "TQQQ_MA200":
            above_ma = len(src) >= 200 and src[-1] > float(np.mean(src[-200:]))
            rising = rising and above_ma
        entry = er is not None and rising and er >= cfg_v5["V5_STRONG_ER_MIN"]
        # Hysteresis: once in STRONG_UP, stay while er stays above the (lower)
        # exit level and the path is still rising; plus a minimum hold.
        # Without these the raw signal flickers (a third of its episodes last
        # 1-2 days), and each flicker is a round-trip trade.
        was_strong = state.bucket == "STRONG_UP"
        stay = (was_strong and er is not None and rising
                and er >= cfg_v5.get("V5_STRONG_ER_EXIT", cfg_v5["V5_STRONG_ER_MIN"]))
        held = state.strong_hold_days < cfg_v5.get("V5_STRONG_MIN_HOLD", 0)
        strong_up = entry or stay or (was_strong and held)
        state.strong_hold_days = state.strong_hold_days + 1 if (strong_up and was_strong) else 0
    elif cfg_v5.get("V5_STRONG_UP_RULE", "THRESHOLD") in ("VOTE_2", "VOTE_3"):
        need = int(cfg_v5["V5_STRONG_UP_RULE"][-1])
        strong_up = strength_votes(qqq_prices) >= need
    elif cfg_v5.get("V5_STRONG_UP_RULE", "THRESHOLD") != "THRESHOLD":
        strong_up = textbook_strong_up(cfg_v5["V5_STRONG_UP_RULE"], qqq_prices)
    # TQQQ_GRADED sizes its leverage by the vote count; carried on the signal
    # dict so bucket_sleeve can read it.
    if cfg_v5.get("V5_STRONG_UP_ASSET") == "TQQQ_GRADED":
        qqq_sig["strength_votes"] = strength_votes(qqq_prices)

    # Optional seasoning: STRONG_UP only once the up-family has held for more
    # than V5_STRONG_AFTER_DAYS days in a row; a younger uptrend is plain UP.
    # Most losing STRONG_UP stints were false breakouts that ended within a
    # few weeks, so leverage waits until the trend has proven itself.
    state.up_age = state.up_age + 1 if up_family else 0
    season = cfg_v5.get("V5_STRONG_AFTER_DAYS", 0)
    if season and state.up_age <= season:
        strong_up = False

    if up_family:
        bucket = "STRONG_UP" if strong_up else "UP"
    elif down_family:
        bucket = "STRONG_DOWN" if strong_down else "DOWN"
    else:
        bucket = "SIDEWAYS"

    # Optional early-warning DOWN: a volatility spike (20-day realized vol
    # above V5_VOLSPIKE_MULT x its own 252-day median) while below the 50-day
    # average turns any non-down day into DOWN. V5's own DOWN fires late --
    # 3% under the MA200 plus confirmation -- and this was the only one of ten
    # faster triggers studied on 2000-2015 (research/early_down_signal.py)
    # that cut max drawdown in every period; it costs some return in most.
    if cfg_v5.get("V5_EARLY_DOWN", "NONE") == "VOLSPIKE" and bucket not in ("DOWN", "STRONG_DOWN"):
        if volatility_spike(qqq_prices, cfg_v5["V5_VOLSPIKE_MULT"]):
            bucket = "DOWN"

    # Optional choppy-market master switch: when the price crossed its 200-day
    # average at least V5_CHOP_CROSSINGS times over the last 252 days, the
    # strategy stands aside and holds SIDEWAYS (all GPIQ). V5_CHOP_SCOPE
    # "UP" only overrides the up-family; "ALL" also overrides the defense.
    k = cfg_v5.get("V5_CHOP_CROSSINGS", 0)
    if k and ma200_crossings(qqq_prices) >= k:
        if cfg_v5.get("V5_CHOP_SCOPE", "UP") == "ALL" or bucket in ("UP", "STRONG_UP"):
            bucket = "SIDEWAYS"

    # Seven-level refinement runs AFTER the five-level call, so FIVE mode and
    # every streak counter above are untouched by it.
    bucket_mode = cfg_v5.get("V5_BUCKET_MODE", "FIVE")
    if bucket_mode == "RATIO_BAND" and bucket == "SIDEWAYS":
        if ratio >= cfg_v5["V5_CHOPPY_UP_RATIO"]:
            bucket = "CHOPPY_UP"
        elif ratio <= cfg_v5["V5_CHOPPY_DOWN_RATIO"]:
            bucket = "CHOPPY_DOWN"
    elif bucket_mode == "EFFICIENCY" and bucket in ("UP", "DOWN"):
        er = efficiency_ratio(qqq_prices, cfg_v5["V5_CHOPPY_ER_LOOKBACK"])
        if er is not None and er < cfg_v5["V5_CHOPPY_ER_MAX"]:
            bucket = "CHOPPY_UP" if bucket == "UP" else "CHOPPY_DOWN"
    elif bucket_mode not in BUCKET_MODES:
        raise AssertionError(f"unreachable V5_BUCKET_MODE {bucket_mode!r}")

    state.bucket = bucket
    return bucket, qqq_sig


def textbook_strong_up(rule, prices):
    """One of the fixed-window STRONG_UP rules (see STRONG_UP_RULES) on the
    prices passed in (strictly before today). False without enough history
    (252 days of returns plus a 252-day vol median for CALM_TREND)."""
    if rule == "ALL_UP":
        return True
    p = np.asarray(prices, dtype=float)
    if len(p) < 253:
        return False

    def ma(n, back=0):
        end = len(p) - back
        return float(np.mean(p[end - n:end]))

    def ret(n):
        return p[-1] / p[-1 - n] - 1.0

    def mom_all():
        return all(ret(n) > 0 for n in (21, 63, 126, 252))

    def calm_trend():
        if len(p) < 252 + 21:
            return False
        lr = np.diff(np.log(p[-(252 + 21):]))
        rolling = np.array([lr[i - 20:i].std(ddof=1) for i in range(20, len(lr) + 1)])
        return p[-1] > ma(50) and p[-1] > ma(200) and rolling[-1] < float(np.median(rolling[-252:]))

    if rule == "MA_STACK":
        if len(p) < 220:
            return False
        return (p[-1] > ma(20) > ma(50) > ma(200)
                and ma(50) > ma(50, back=20) and ma(200) > ma(200, back=20))
    if rule == "MOM_ALL":
        return mom_all()
    if rule == "NEAR_HIGH":
        return p[-1] >= 0.95 * float(np.max(p[-252:]))
    if rule == "CALM_TREND":
        return calm_trend()
    if rule == "MOM_CALM":
        return mom_all() and calm_trend()
    raise AssertionError(f"unreachable STRONG_UP rule {rule!r}")


def ma200_crossings(prices, window=252):
    """How many times the close crossed its 200-day average over the last
    `window` days (0 without enough history)."""
    p = np.asarray(prices, dtype=float)
    if len(p) < window + 200:
        return 0
    c = np.cumsum(np.r_[0.0, p])
    ma = (c[200:] - c[:-200]) / 200.0            # ma[i] = mean(p[i:i+200])
    above = p[199:][-window:] > ma[-window:]
    return int(np.count_nonzero(above[1:] != above[:-1]))


def strength_votes(prices):
    """How many of the four textbook strength signals (VOTE_SIGNALS) hold
    today, 0-4."""
    return sum(textbook_strong_up(rule, prices) for rule in VOTE_SIGNALS)


def volatility_spike(prices, mult, vol_window=20, median_window=252, ma_window=50):
    """True if the last `vol_window`-day realized vol is above `mult` x the
    median of that rolling vol over the previous `median_window` days AND the
    last close is below its `ma_window`-day average. False if there isn't
    enough history. Uses only the prices passed in (strictly before today)."""
    need = median_window + vol_window + 1
    if len(prices) < need:
        return False
    p = np.asarray(prices[-need:], dtype=float)
    lr = np.diff(np.log(p))
    rolling = np.array([lr[i - vol_window:i].std(ddof=1) for i in range(vol_window, len(lr) + 1)])
    today_vol = rolling[-1]
    median_vol = float(np.median(rolling[-median_window:]))
    below_ma = p[-1] < float(np.mean(p[-ma_window:]))
    return bool(today_vol > mult * median_vol and below_ma)


def efficiency_ratio(prices, lookback):
    """Kaufman efficiency ratio over the last `lookback` days: |net move| /
    sum of |daily moves|. 1.0 = a straight line, near 0 = lots of back-and-forth
    for little net progress. None if there isn't enough history."""
    if len(prices) < lookback + 1:
        return None
    seg = np.asarray(prices[-(lookback + 1):], dtype=float)
    path = float(np.abs(np.diff(seg)).sum())
    if path <= 0:
        return 0.0
    return abs(float(seg[-1] - seg[0])) / path


def choppy_blend_t(cfg_v5, bucket, qqq_sig, er=None):
    """How far a CHOPPY_UP / CHOPPY_DOWN day leans toward its trending
    neighbour (UP / DOWN), in [0, 1]: 0 = hold exactly what SIDEWAYS holds,
    1 = hold exactly what UP/DOWN holds.

    RATIO_BAND: driven by QQQ's ratio to its MA200 across the band between the
    SIDEWAYS centre (1.0) and the UP/DOWN entry ratio, so the position slides
    smoothly from SIDEWAYS through CHOPPY_UP into UP (and mirror-wise down).
    EFFICIENCY: a CHOPPY day there already sits past the UP/DOWN entry ratio,
    so the ratio axis would always give 1; instead t = er / V5_CHOPPY_ER_MAX,
    i.e. the noisier the path, the more it leans back toward SIDEWAYS."""
    if cfg_v5.get("V5_BUCKET_MODE") == "EFFICIENCY":
        er = 0.0 if er is None else er
        return float(np.clip(er / cfg_v5["V5_CHOPPY_ER_MAX"], 0.0, 1.0))
    ratio = qqq_sig["ratio"]
    if bucket == "CHOPPY_UP":
        lo, hi = 1.0, cfg_v5["V5_UP_RATIO"]
        return float(np.clip((ratio - lo) / (hi - lo), 0.0, 1.0))
    if bucket == "CHOPPY_DOWN":
        hi, lo = 1.0, cfg_v5["V5_DOWN_RATIO"]
        return float(np.clip((hi - ratio) / (hi - lo), 0.0, 1.0))
    raise AssertionError(f"choppy_blend_t called for non-choppy bucket {bucket!r}")


def spmo_slot_signal(cfg_v5, state, spmo_prices):
    """Decides which ticker occupies the UP bucket's risk slot, judged
    ENTIRELY on SPMO's own trend qualification (its own ratio vs its own
    entry/exit thresholds) -- NOT a QQQ-vs-SPMO head-to-head comparison (that
    is state_machine.stronger_asset()'s job, not used here). Returns
    (slot, spmo_sig) where slot is "SPMO" or cfg_v5["V5_NDX_TICKER"] (the
    Nasdaq-100 fund V5 holds, QQQM); (state.spmo_slot or that ticker, None) if
    SPMO's own history isn't long enough yet (conservative fallback to the
    Nasdaq-100 fund, the same shape as every other insufficient-history path
    in this codebase)."""
    ndx = cfg_v5["V5_NDX_TICKER"]
    spmo_sig = sm.signals(cfg_v5, spmo_prices)
    if spmo_sig is None:
        return (state.spmo_slot or ndx), None
    confirm_days = cfg_v5["V5_CONFIRM_DAYS"]
    entered = _confirm_v5(state, "spmo_bull", spmo_sig["ratio"] >= cfg_v5["V5_SPMO_ENTRY_RATIO"], confirm_days)
    exited = _confirm_v5(state, "spmo_exit", spmo_sig["ratio"] < cfg_v5["V5_SPMO_EXIT_RATIO"], confirm_days)
    if state.spmo_slot == "SPMO":
        state.spmo_slot = ndx if exited else "SPMO"
    else:
        state.spmo_slot = "SPMO" if entered else ndx
    return state.spmo_slot, spmo_sig


def up_bucket_tilt(cfg_v5, qqq_sig):
    """Continuous GPIQ-vs-risk-ticker split of the UP bucket's discretionary
    slice, interpolated between the UP bucket's own two boundary ratios
    (V5_UP_RATIO on entry from SIDEWAYS, V5_STRONG_UP_RATIO on the edge of
    flipping to STRONG_UP) -- the same interpolation SHAPE as
    state_machine.tilt_attack_weights(), no new threshold concept introduced.
    Returns gpiq_share: the fraction OF THE DISCRETIONARY SLICE given to
    GPIQ (the risk ticker gets 1 - gpiq_share); 0.7 just inside UP, 0.2 right
    at the STRONG_UP edge."""
    lo, hi = cfg_v5["V5_UP_RATIO"], cfg_v5["V5_STRONG_UP_RATIO"]
    t = float(np.clip((qqq_sig["ratio"] - lo) / (hi - lo), 0.0, 1.0))
    gpiq_max, gpiq_min = cfg_v5["V5_UP_GPIQ_MAX"], cfg_v5["V5_UP_GPIQ_MIN"]
    gpiq_share = gpiq_max - t * (gpiq_max - gpiq_min)
    return gpiq_share, t


def cash_ratio_scale(cfg_v5, mode, bucket, qqq_sig, up_t, realized_annual_vol, risk_ticker=None, leverage=None):
    """Returns invest_scale in [V5_CASH_MIN_SCALE, 1.0]: the fraction of the
    discretionary slice (after the GPIQ floor is carved out) that stays
    invested rather than idle cash. Two independent, non-blended mechanisms
    (per the approved plan -- never hybridized):

    "VOL_TARGET": scales down as the currently-held risk ticker's own
    realized volatility rises above an EFFECTIVE target -- V5_VOL_TARGET_ANNUAL
    scaled by risk_ticker's own leverage multiplier (V5_TICKER_LEVERAGE_MULTIPLIER),
    since a ticker-blind global target was found to permanently starve TQQQ/
    QID's invest_scale (their own realized vol runs ~2-3x QQQ's by design,
    pinning them near V5_CASH_MIN_SCALE regardless of actual trend strength).
    `risk_ticker=None` (the default) falls back to multiplier 1.0, exactly
    reproducing the pre-this-change formula -- existing callers that never
    pass this parameter are unaffected. Same formula SHAPE as the old
    track's engine._vol_scale_weights(), reimplemented independently here
    (see engine_v5._realized_annual_vol()) so the two tracks share no code.
    `realized_annual_vol` is precomputed by the caller (engine_v5, which owns
    the closes panel) and passed in.

    "SIGNAL_STRENGTH": for UP/DOWN, reuses up_bucket_tilt's own `up_t` (0 at
    the bucket-entry boundary, 1 at the strong boundary) directly. For
    STRONG_UP/STRONG_DOWN (beyond the strong boundary, where up_t is not
    defined), computes an analogous "how far past the strong boundary" ramp
    saturating over V5_SIGNAL_SATURATION_BAND. For SIDEWAYS, always 1.0 (no
    risk ticker to size -- the whole discretionary slice is GPIQ regardless
    of invest_scale, so the value is inert but kept well-defined). Ignores
    risk_ticker entirely -- this mode never keys off ticker identity."""
    min_scale = cfg_v5["V5_CASH_MIN_SCALE"]
    # Optional: buckets listed in V5_FULL_INVEST_BUCKETS skip the cash/equity
    # scaling entirely and stay fully invested (only the CASH_BUFFER stays as
    # cash) -- e.g. UP/STRONG_UP, where de-risking on volatility was found to
    # keep a median ~13% of the slice in cash during confirmed uptrends.
    if bucket in cfg_v5.get("V5_FULL_INVEST_BUCKETS", ()):
        return 1.0
    if mode == "VOL_TARGET":
        return vol_target_scale(cfg_v5, realized_annual_vol, risk_ticker, leverage)
    if mode == "SIGNAL_STRENGTH":
        if bucket in ("UP", "DOWN"):
            t = up_t if bucket == "UP" else _down_bucket_t(cfg_v5, qqq_sig)
            return min_scale + t * (1.0 - min_scale)
        if bucket in ("CHOPPY_UP", "CHOPPY_DOWN"):
            # Weakest trend conviction short of SIDEWAYS: the same floor UP/DOWN
            # use right at their entry boundary. (SIDEWAYS itself stays 1.0 --
            # its slice is all GPIQ, so invest_scale only sizes the trend half
            # of the blend in effective_weights_v5.)
            return min_scale
        if bucket in ("STRONG_UP", "STRONG_DOWN"):
            band = cfg_v5["V5_SIGNAL_SATURATION_BAND"]
            if bucket == "STRONG_UP":
                boundary = cfg_v5["V5_STRONG_UP_RATIO"]
                overshoot = qqq_sig["ratio"] - boundary
            else:
                boundary = cfg_v5["V5_STRONG_DOWN_RATIO"]
                overshoot = boundary - qqq_sig["ratio"]
            t = float(np.clip(overshoot / band, 0.0, 1.0))
            return min_scale + t * (1.0 - min_scale)
        return 1.0  # SIDEWAYS: inert, no risk ticker to size.
    raise AssertionError(f"unreachable CASH_RATIO_MODE {mode!r}")


def vol_target_scale(cfg_v5, realized_annual_vol, risk_ticker=None, leverage=None):
    """VOL_TARGET's invested fraction in [V5_CASH_MIN_SCALE, 1.0]: the
    effective target (V5_VOL_TARGET_ANNUAL x risk_ticker's leverage) over the
    realized vol; 1.0 when there is no volatility reading. Also decides how
    much of a CASCADE trimmed slice each lower level accepts. `leverage`
    (e.g. sleeve_leverage of a mixed TQQQ/QQQM sleeve) overrides the
    risk_ticker lookup."""
    if realized_annual_vol is None or realized_annual_vol <= 0:
        return 1.0
    multiplier = leverage if leverage is not None else V5_TICKER_LEVERAGE_MULTIPLIER.get(risk_ticker, 1.0)
    effective_target = cfg_v5["V5_VOL_TARGET_ANNUAL"] * multiplier
    return float(np.clip(effective_target / realized_annual_vol, cfg_v5["V5_CASH_MIN_SCALE"], 1.0))


def _down_bucket_t(cfg_v5, qqq_sig):
    """DOWN bucket's own entry->strong-boundary interpolation parameter,
    symmetric with up_bucket_tilt's `t` (0 at V5_DOWN_RATIO, 1 at
    V5_STRONG_DOWN_RATIO). Not used for a GPIQ tilt (DOWN holds no GPIQ
    beyond the fixed floor -- PSQ is a full discrete swap), only as the
    SIGNAL_STRENGTH cash-ratio driver for the DOWN bucket."""
    lo, hi = cfg_v5["V5_DOWN_RATIO"], cfg_v5["V5_STRONG_DOWN_RATIO"]
    return float(np.clip((lo - qqq_sig["ratio"]) / (lo - hi), 0.0, 1.0))


def effective_weights_v5(cfg_v5, bucket, qqq_sig, spmo_slot, invest_scale, er=None, spmo_sig=None,
                         level_scales=None):
    """Target {ticker: weight} for one day (cash is 1 - sum). CHOPPY_UP /
    CHOPPY_DOWN hold a continuous blend of their two neighbours' targets,
    weighted by choppy_blend_t(): t * (UP or DOWN) + (1 - t) * SIDEWAYS. Both
    neighbours are built by _bucket_weights() from the same invest_scale, so
    the GPIQ floor and the invested total carry over to the blend exactly.
    `er` (QQQ's efficiency ratio) only matters in EFFICIENCY bucket mode;
    `spmo_sig` (SPMO's own signals) only matters for a TREND sleeve split;
    `level_scales` only for a CASCADE derisk asset (see _bucket_weights)."""
    if bucket in ("CHOPPY_UP", "CHOPPY_DOWN"):
        t = choppy_blend_t(cfg_v5, bucket, qqq_sig, er)
        neighbour = "UP" if bucket == "CHOPPY_UP" else "DOWN"
        trend = _bucket_weights(cfg_v5, neighbour, qqq_sig, spmo_slot, invest_scale, spmo_sig, level_scales)
        flat = _bucket_weights(cfg_v5, "SIDEWAYS", qqq_sig, spmo_slot, invest_scale, spmo_sig)
        blended = {}
        for ticker in set(trend) | set(flat):
            w = t * trend.get(ticker, 0.0) + (1.0 - t) * flat.get(ticker, 0.0)
            if w > 0:
                blended[ticker] = w
        return blended
    return _bucket_weights(cfg_v5, bucket, qqq_sig, spmo_slot, invest_scale, spmo_sig, level_scales)


def _bucket_weights(cfg_v5, bucket, qqq_sig, spmo_slot, invest_scale, spmo_sig=None, level_scales=None):
    """Composes the GPIQ floor, the UP-bucket continuous tilt, and the
    cash/equity invest_scale into a final {ticker: weight} dict (CASH is
    implicit: 1 - sum(weights.values())). GPIQ's weight is set to the floor
    FIRST and only ever added to -- the floor invariant is true by
    construction for every bucket, not a post-hoc clamp.

    risk ticker per bucket: see bucket_risk_ticker() (UP -> spmo_slot;
    STRONG_UP/DOWN/STRONG_DOWN -> their configured V5_*_ASSET, defaults
    TQQQ/PSQ/QID; SIDEWAYS -> none, GPIQ absorbs the whole discretionary
    slice). A CASH asset leaves its slice uninvested."""
    gpiq_floor = cfg_v5["V5_GPIQ_FLOOR"]
    cash_buffer = cfg_v5["V5_CASH_BUFFER"]
    discretionary_max = 1.0 - gpiq_floor - cash_buffer
    invested_discretionary = discretionary_max * invest_scale

    weights = {"GPIQ": gpiq_floor}
    if bucket == "SIDEWAYS":
        weights["GPIQ"] += invested_discretionary
        return weights
    if bucket == "UP":
        gpiq_share, _ = up_bucket_tilt(cfg_v5, qqq_sig)
    elif bucket in ("STRONG_UP", "DOWN", "STRONG_DOWN"):
        gpiq_share = 0.0
    else:
        raise AssertionError(f"unreachable bucket {bucket!r}")
    sleeve = bucket_sleeve(cfg_v5, bucket, spmo_slot, qqq_sig, spmo_sig)

    weights["GPIQ"] += invested_discretionary * gpiq_share
    risk_weight = invested_discretionary * (1.0 - gpiq_share)
    if risk_weight > 0:
        for ticker, share in sleeve.items():
            if share > 0:
                weights[ticker] = weights.get(ticker, 0.0) + risk_weight * share
    # The slice the cash/equity scaling trimmed off: idle cash by default, or
    # parked in V5_UP_DERISK_ASSET / V5_STRONG_DERISK_ASSET (see DERISK_ASSETS).
    trimmed = discretionary_max - invested_discretionary
    asset = derisk_asset(cfg_v5, bucket)
    if trimmed > 0 and asset not in (None, "CASH"):
        if asset == "CASCADE":
            # Step down one level at a time: each level keeps the fraction its
            # own volatility target allows (level_scales, one per level from
            # the caller; 1.0 = all of it) and passes the rest on; the
            # remainder at the bottom sits in T-bills rather than idle cash.
            levels = derisk_levels(cfg_v5, bucket, spmo_slot, qqq_sig, spmo_sig)
            scales = list(level_scales) if level_scales is not None else [1.0] * len(levels)
            for sleeve_l, scale_l in zip(levels, scales):
                taken = trimmed * scale_l
                for ticker, share in sleeve_l.items():
                    if share > 0:
                        weights[ticker] = weights.get(ticker, 0.0) + taken * share
                trimmed -= taken
            if trimmed > 0:
                weights["SGOV"] = weights.get("SGOV", 0.0) + trimmed
        else:
            ticker = cfg_v5["V5_NDX_TICKER"] if asset == "NDX" else asset
            weights[ticker] = weights.get(ticker, 0.0) + trimmed
    return weights


def effective_targets_v5(cfg_v5, weights, today):
    """Routes every not-yet-listed ticker's weight through its pre-listing
    proxy (cfg_v5["V5_PROXIES"]: GPIQ -> QQQM before 2023-11-01, QQQM -> QQQ
    before 2020-10-13, SGOV -> BIL before 2020-06-01), like
    state_machine.effective_targets() does for the old track, but following
    chains. Every V5 bucket holds a GPIQ floor across all three periods,
    including DEV_EARLY and FINAL_HOLDOUT, both before GPIQ existed. Returns
    (targets, proxy_weight): targets is {actual_ticker: weight} (a proxy that
    is also held directly is merged), proxy_weight is the total fraction
    currently routed through a substitute."""
    today = today if isinstance(today, date) else today.date()
    targets = {}
    proxy_weight = 0.0
    for ticker, weight in weights.items():
        actual = tradeable_ticker(cfg_v5, ticker, today)
        targets[actual] = targets.get(actual, 0.0) + weight
        if actual != ticker:
            proxy_weight += weight
    return targets, proxy_weight


def tradeable_ticker(cfg_v5, ticker, today):
    """The ticker actually traded for `ticker` on `today`: follows the
    V5_PROXIES chain while the current link isn't listed yet (GPIQ -> QQQM ->
    QQQ before QQQM's 2020-10 listing). Used for both the holdings and the
    VOL_TARGET volatility lookup, so volatility is always measured on what is
    really held -- an unlisted ticker has no prices, and measuring it would
    silently disable the volatility control."""
    today = today if isinstance(today, date) else today.date()
    proxies = cfg_v5["V5_PROXIES"]
    actual = ticker
    for _ in range(len(proxies) + 1):
        if actual in proxies and today < date(*proxies[actual][0]):
            actual = proxies[actual][1]
        else:
            return actual
    raise ValueError(f"V5_PROXIES has a cycle starting at {ticker!r}")

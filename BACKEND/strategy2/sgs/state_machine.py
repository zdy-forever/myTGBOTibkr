"""QQQ regime state machine, ported 1:1 from reference/SGS_V3_2_BASE_main.py
SGSQQQIncomeRotationResearch.daily_review() / _confirm() / _effective_targets().

Kept as pure functions operating on an explicit StreakState object (instead of
instance attributes on a QCAlgorithm) so the transition logic can be unit
tested independently of any account/engine machinery.

Signal confirmation counters ("streaks") are recomputed every trading day for
EVERY condition, even while not in the associated regime, exactly as the
original comment says: "Experimental conditions are computed every trading
day, even while not in the associated regime, so confirmations have true
continuity." Do not special-case this away.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np

STREAK_LABELS = (
    "below", "bull", "recovering", "attack_exit", "weak",
    "early_bounce", "early_full", "early_fail", "weak_recover",
    "spmo_bull", "spmo_exit",
)


@dataclass
class StreakState:
    regime: str | None = None
    recovery_days: int = 0
    signal_streak: dict = field(default_factory=lambda: {k: 0 for k in STREAK_LABELS})
    held_asset: str | None = None
    asset_candidate_streak: int = 0


def _confirm(state, label, condition, confirm_days, days=None):
    state.signal_streak[label] = state.signal_streak[label] + 1 if condition else 0
    return state.signal_streak[label] >= (confirm_days if days is None else days)


def signals(cfg, prices):
    """prices: 1-D array of completed daily closes, prices[-1] = most recent
    completed bar (yesterday, relative to the decision day). Needs >= 201
    points (same floor as the original _qqq_history length check)."""
    if len(prices) < 201 or not np.all(np.isfinite(prices)):
        return None
    p = np.asarray(prices, dtype=float)
    close = float(p[-1])
    ret1 = close / p[-2] - 1.0
    ret5 = close / p[-6] - 1.0
    ret63 = close / p[-64] - 1.0
    ret20 = close / p[-21] - 1.0
    bounce20 = close / float(np.min(p[-20:])) - 1.0
    ma20 = float(np.mean(p[-20:]))
    ma50 = float(np.mean(p[-50:]))
    ma200 = float(np.mean(p[-200:]))
    ratio = close / ma200
    dd126 = close / float(np.max(p[-126:])) - 1.0
    return dict(close=close, ret1=ret1, ret5=ret5, ret63=ret63, ret20=ret20,
                bounce20=bounce20, ma20=ma20, ma50=ma50, ma200=ma200,
                ratio=ratio, dd126=dd126)


def spmo_bull_signal(cfg, state, spmo_prices):
    """SPMO's own bull confirmation, reusing the identical formula/thresholds
    as the QQQ bull condition in step() (BULL_MA200_RATIO/BULL_RETURN_63/
    CONFIRM_DAYS) so this stays a pure trend-source swap rather than a new
    parameter space. Tracked with its own "spmo_bull" streak so it has true
    day-to-day continuity independent of whatever the QQQ-driven bull streak
    is doing. Returns (confirmed, sig) or (None, None) if SPMO's own history
    is not yet long enough (the same conservative-failure shape as step()).
    """
    sig = signals(cfg, spmo_prices)
    if sig is None:
        return None, None
    confirmed = _confirm(state, "spmo_bull", (
        sig["ratio"] > cfg["BULL_MA200_RATIO"] and sig["ma50"] > sig["ma200"]
        and sig["ret63"] > cfg["BULL_RETURN_63"]
    ), cfg["CONFIRM_DAYS"])
    return confirmed, sig


def spmo_exit_signal(cfg, state, spmo_prices):
    """SPMO's own ATTACK-exit confirmation, formula-symmetric with the QQQ
    weak_exit condition in step() (EXIT_MA50_RATIO/EXIT_RETURN_63/
    EXIT_DRAWDOWN_126/CONFIRM_DAYS) but driven entirely by SPMO's own price
    series. Used by DUAL so the exit decision for the SPMO-driven ATTACK
    entry is judged on SPMO's own terms rather than mixing in QQQ's weak_exit
    (that mix was found to cause daily whipsaws when QQQ chops around its
    exit thresholds while SPMO itself is still confirmed strong -- see the
    DEV_RECENT 2026-07 state audit). Tracked with its own "spmo_exit" streak.
    Returns (confirmed, sig) or (None, None) if SPMO's own history is not yet
    long enough (same conservative-failure shape as step())."""
    sig = signals(cfg, spmo_prices)
    if sig is None:
        return None, None
    confirmed = _confirm(state, "spmo_exit", (
        sig["close"] < cfg["EXIT_MA50_RATIO"] * sig["ma50"]
        or sig["ret63"] < cfg["EXIT_RETURN_63"]
        or sig["dd126"] <= cfg["EXIT_DRAWDOWN_126"]
    ), cfg["CONFIRM_DAYS"])
    return confirmed, sig


def tilt_attack_weights(cfg, role_weights_attack, spmo_sig):
    """Continuous GPIQ/SPMO split inside ATTACK, driven by SPMO's own
    ratio-to-its-MA200 (reusing BULL_MA200_RATIO/BEAR_MA200_RATIO as the two
    interpolation endpoints -- no new threshold parameters). At or above the
    bull ratio, SPMO keeps its full original share; at or below the bear
    ratio, SPMO's share is wound down to SPMO_TILT_MIN_WEIGHT and GPIQ
    absorbs the difference. GPIQ+SPMO always sums to the original total."""
    total = role_weights_attack["GPIQ"] + role_weights_attack["SPMO"]
    max_spmo = role_weights_attack["SPMO"]
    min_spmo = cfg["SPMO_TILT_MIN_WEIGHT"]
    lo, hi = cfg["BEAR_MA200_RATIO"], cfg["BULL_MA200_RATIO"]
    t = float(np.clip((spmo_sig["ratio"] - lo) / (hi - lo), 0.0, 1.0))
    spmo_weight = min_spmo + t * (max_spmo - min_spmo)
    return {"GPIQ": total - spmo_weight, "SPMO": spmo_weight}


def stronger_asset(cfg, qqq_prices, spmo_prices):
    """Picks whichever of QQQ/SPMO is "stronger" today, for ATTACK_ASSET=
    "STRONGER" (engine.py's ATTACK/NEUTRAL/RECOVERY asset-swap hook). This is
    a stateless, same-day comparison -- not a multi-day confirmed signal like
    bull/weak_exit -- so it takes no StreakState and is not called from
    step(). Returns ("QQQ" or "SPMO", qqq_sig, spmo_sig), or (None, None,
    None) if either side's history isn't long enough yet (same conservative-
    failure shape as elsewhere in this module).

    cfg["ATTACK_ASSET_COMPARISON"] picks the comparison field, both already
    computed by signals() -- no new formula introduced:
    "RATIO": signals()["ratio"] (close/MA200); "RET20": signals()["ret20"].
    Ties go to QQQ."""
    qsig, ssig = signals(cfg, qqq_prices), signals(cfg, spmo_prices)
    if qsig is None or ssig is None:
        return None, None, None
    field = "ratio" if cfg["ATTACK_ASSET_COMPARISON"] == "RATIO" else "ret20"
    winner = "QQQ" if qsig[field] >= ssig[field] else "SPMO"
    return winner, qsig, ssig


def resolve_attack_asset(cfg, state, target_regime, old_regime, qqq_prices, spmo_prices):
    """Decides which of SPMO/QQQ to hold today under ATTACK_ASSET="STRONGER",
    applying cfg["ASSET_SWITCH_MODE"] to suppress whipsaw-y daily flips
    between the two (see the real 2019-08 episode: 9 flips in a month under
    plain day-by-day "whoever's stronger wins"). Only meaningful for
    target_regime in (ATTACK, NEUTRAL, RECOVERY); callers outside that set
    should not call this. Mutates state.held_asset / state.asset_candidate_streak.

    Returns (winner, qsig, ssig), the same shape as stronger_asset() --
    (None, None, None) means "not enough history yet, leave role_override
    untouched" (the same conservative-failure shape used everywhere else in
    this module).

    old_regime: the regime on the PRIOR trading day (before today's step()
    call), used only by LOCK_PER_EPISODE to detect "just entered the
    ATTACK/NEUTRAL/RECOVERY group from outside it"."""
    mode = cfg.get("ASSET_SWITCH_MODE", "DAILY")
    risk_on = ("ATTACK", "NEUTRAL", "RECOVERY")
    entering_risk_on = target_regime in risk_on and old_regime not in risk_on

    if mode == "LOCK_PER_EPISODE":
        if entering_risk_on or state.held_asset is None:
            winner, qsig, ssig = stronger_asset(cfg, qqq_prices, spmo_prices)
            if winner is not None:
                state.held_asset = winner
            return winner, qsig, ssig
        # Still inside the risk-on group (including ATTACK<->NEUTRAL<->
        # RECOVERY churn) -- keep the locked asset. Still compute sig dicts
        # for callers/audits that want them; never touch held_asset.
        qsig, ssig = signals(cfg, qqq_prices), signals(cfg, spmo_prices)
        return state.held_asset, qsig, ssig

    winner, qsig, ssig = stronger_asset(cfg, qqq_prices, spmo_prices)
    if winner is None:
        return None, None, None

    if mode == "DAILY":
        state.held_asset = winner
        return winner, qsig, ssig

    if mode == "CONFIRM_N":
        if winner == state.held_asset or state.held_asset is None:
            state.asset_candidate_streak = 0
            state.held_asset = winner
        else:
            state.asset_candidate_streak += 1
            if state.asset_candidate_streak >= cfg["ASSET_SWITCH_CONFIRM_DAYS"]:
                state.held_asset = winner
                state.asset_candidate_streak = 0
        return state.held_asset, qsig, ssig

    if mode == "MARGIN_BAND":
        if state.held_asset is None:
            state.held_asset = winner
            return winner, qsig, ssig
        field = "ratio" if cfg["ATTACK_ASSET_COMPARISON"] == "RATIO" else "ret20"
        held_value = qsig[field] if state.held_asset == "QQQ" else ssig[field]
        other = "QQQ" if state.held_asset == "SPMO" else "SPMO"
        other_value = qsig[field] if other == "QQQ" else ssig[field]
        margin = cfg["ASSET_SWITCH_MARGIN"]
        if other_value > held_value * (1 + margin):
            state.held_asset = other
        return state.held_asset, qsig, ssig

    raise AssertionError(f"unreachable ASSET_SWITCH_MODE {mode!r}")


def step(cfg, state, prices, spmo_bull_confirmed=None, spmo_exit_confirmed=None):
    """Advance the state machine by one trading day.

    Returns (target_regime, sig) where sig is the dict from signals(), or
    (None, None) if there isn't enough price history yet (mirrors the
    original's early-return when _qqq_history() is None). Mutates `state`
    in place (streak counters, recovery_days, regime) exactly like the
    original instance-attribute version; callers own the StreakState
    instance across days.

    spmo_bull_confirmed: result of spmo_bull_signal() for the SAME day,
    computed by the caller BEFORE calling step() (both functions advance
    their own streak counters on `state`, so call order matters). Only
    consulted when cfg["SPMO_MODE"] is "GATE" or "DUAL"; ignored otherwise.
    If None (SPMO history not yet long enough), the GATE/DUAL override is
    inert for that day and falls back to the plain QQQ bull signal, the same
    conservative-failure shape as everywhere else in this module.

    spmo_exit_confirmed: result of spmo_exit_signal() for the SAME day
    (computed by the caller, same ordering requirement as above). Only
    consulted when cfg["SPMO_MODE"]=="DUAL": DUAL's ATTACK exit is judged
    ENTIRELY on this (QQQ's own weak_exit is not consulted at all for DUAL),
    since mixing "exit on QQQ OR SPMO" caused same-day whipsaws whenever QQQ
    chopped around its exit thresholds while SPMO was still confirmed
    strong. If None (SPMO history not yet long enough), DUAL's exit falls
    back to QQQ's weak_exit for that day, the conservative-failure shape
    used everywhere else in this module. GATE is unaffected by this
    parameter -- it still only gates ENTRY, exit for GATE is unchanged.
    """
    sig = signals(cfg, prices)
    if sig is None:
        return None, None
    close, ma20, ma50, dd126, ratio = sig["close"], sig["ma20"], sig["ma50"], sig["dd126"], sig["ratio"]
    ret1, ret5, ret20, ret63, bounce20 = sig["ret1"], sig["ret5"], sig["ret20"], sig["ret63"], sig["bounce20"]
    confirm_days = cfg["CONFIRM_DAYS"]
    old_regime = state.regime

    below = _confirm(state, "below", ratio < 1.0, confirm_days)
    bull = _confirm(state, "bull", (
        ratio > cfg["BULL_MA200_RATIO"] and ma50 > sig["ma200"] and ret63 > cfg["BULL_RETURN_63"]
    ), confirm_days)
    weak_exit = _confirm(state, "attack_exit", (
        close < cfg["EXIT_MA50_RATIO"] * ma50
        or ret63 < cfg["EXIT_RETURN_63"]
        or dd126 <= cfg["EXIT_DRAWDOWN_126"]
    ), confirm_days)
    weak = _confirm(state, "weak", (
        close < ma50 and ret63 < cfg["WEAK_RETURN_63"] and dd126 <= cfg["WEAK_DRAWDOWN_126"]
    ), confirm_days)

    # BEAR entry is NEVER modified by BEAR_LOGIC -- a same-day drawdown-depth
    # gate on the shallow triggers (ratio below BEAR_MA200_RATIO, or the
    # "below" streak alone) was tried and rejected: dd126 is a 126-day
    # peak-to-trough measure that lags a fast-moving ratio signal, so the gate
    # delays entry more the SLOWER a decline is, and for a genuinely slow
    # decline it can fail to ever confirm before the decline reverses on its
    # own. See BASE_CFG's RECOVERY_RET63_MIN comment for the full trail.
    bear = (
        dd126 <= cfg["BEAR_DRAWDOWN_126"]
        or ratio < cfg["BEAR_MA200_RATIO"]
        or state.signal_streak["below"] >= cfg["BEAR_CONFIRM_DAYS"]
    )

    # BEAR_LOGIC="RECOVERY_ONLY_IMPROVED" adds a second, independent
    # recovering path (63-day momentum back positive, plus price back above
    # its own 200-day average) alongside the ORIGINAL ratio-based path --
    # data-validated to only shorten a slow recovery (the real 2018 episode
    # confirmed ~1 week earlier) while reproducing the IDENTICAL confirmation
    # date as ORIGINAL in the real 2025 episode (never pulls forward a
    # genuine crash's recovery call). See BASE_CFG's comment for the full
    # data trail.
    recovery_improved = cfg.get("BEAR_LOGIC") == "RECOVERY_ONLY_IMPROVED"
    recovering = _confirm(state, "recovering", (
        (ratio > cfg["RECOVERY_MA200_RATIO"] and ret63 > cfg["RECOVERY_RETURN_63"])
        or (recovery_improved and ret63 > cfg["RECOVERY_RET63_MIN"] and ratio > 1.0)
    ), confirm_days)
    early_bounce_ready = _confirm(
        state, "early_bounce",
        close > ma20 * cfg["EARLY_MA20_RATIO"]
        and ret5 > cfg["EARLY_RETURN_5"]
        and bounce20 >= cfg["EARLY_LOW20_BOUNCE"],
        confirm_days, cfg["EARLY_CONFIRM_DAYS"],
    )
    early_full_ready = _confirm(
        state, "early_full",
        close > ma50 * cfg["EARLY_TO_FULL_MA50_RATIO"] and ret20 > cfg["EARLY_TO_FULL_RETURN_20"],
        confirm_days, cfg["EARLY_TO_FULL_CONFIRM_DAYS"],
    )
    early_fail_ready = _confirm(
        state, "early_fail",
        close < ma20 * cfg["EARLY_FAIL_MA20_RATIO"] and ret5 < cfg["EARLY_FAIL_RETURN_5"],
        confirm_days, cfg["EARLY_FAIL_CONFIRM_DAYS"],
    )
    weak_recover_ready = _confirm(
        state, "weak_recover",
        close > ma20 * cfg["WEAK_RECOVER_MA20_RATIO"] and ret5 > cfg["WEAK_RECOVER_RETURN_5"],
        confirm_days, cfg["WEAK_RECOVER_CONFIRM_DAYS"],
    )

    sideways = "WEAK" if weak else "NEUTRAL"
    use_early, use_weak = cfg["USE_EARLY"], cfg["USE_WEAK"]

    # SPMO GATE/DUAL: only active when the caller actually supplied a
    # same-day SPMO confirmation (None means SPMO's own history isn't long
    # enough yet, so this degrades to the plain QQQ bull signal for the day).
    spmo_mode = cfg.get("SPMO_MODE")
    active_spmo_mode = spmo_mode if (spmo_mode in ("GATE", "DUAL") and spmo_bull_confirmed is not None) else None

    # bull and weak_exit are NOT mutually exclusive by construction (bull is
    # a strict AND of three conditions, weak_exit is a loose OR of three
    # others), so entering/re-entering ATTACK must explicitly also require
    # "the exit condition isn't simultaneously true" -- otherwise a NEUTRAL
    # day right after an ATTACK exit can immediately re-qualify for ATTACK
    # under bull, producing daily whipsaws. This affects every variant
    # (plain QQQ, SPMO-as-sole-trend-source, GATE, DUAL), not just DUAL:
    # the same whipsaw was found in the SPMO/NONE run (18/47 transitions
    # were 1-day) and to a lesser extent the QQQ baseline (10/36). For DUAL,
    # the relevant exit flag is SPMO's own spmo_exit_confirmed (falling back
    # to weak_exit when SPMO's history isn't long enough yet, matching the
    # ATTACK-maintenance branch below).
    def attack_signal():
        if active_spmo_mode == "DUAL":
            exit_flag = weak_exit if spmo_exit_confirmed is None else spmo_exit_confirmed
            return bool(spmo_bull_confirmed) and not exit_flag
        if active_spmo_mode == "GATE":
            return bull and bool(spmo_bull_confirmed) and not weak_exit
        return bull and not weak_exit

    if use_early and state.regime == "BEAR" and early_bounce_ready:
        target_regime = "EARLY_RECOVERY"
    elif use_early and state.regime == "EARLY_RECOVERY":
        if early_fail_ready or ret5 <= cfg["EARLY_PANIC_RETURN_5"]:
            target_regime = "BEAR"
        elif early_full_ready:
            target_regime = "RECOVERY"
        else:
            target_regime = "EARLY_RECOVERY"
    elif bear:
        target_regime = "BEAR"
    elif state.regime == "BEAR":
        target_regime = "RECOVERY" if recovering else "BEAR"
    elif state.regime == "RECOVERY":
        if state.recovery_days < cfg["RECOVERY_WAIT_DAYS"]:
            state.recovery_days += 1
            target_regime = "RECOVERY"
        else:
            target_regime = "ATTACK" if attack_signal() else sideways
    elif state.regime == "ATTACK":
        if active_spmo_mode == "DUAL":
            # DUAL's exit is judged ENTIRELY on SPMO's own exit signal, not
            # mixed with QQQ's weak_exit -- see spmo_exit_signal()'s docstring
            # for why mixing the two caused daily whipsaws. Falls back to
            # weak_exit only if SPMO's own history isn't long enough yet.
            drop = weak_exit if spmo_exit_confirmed is None else spmo_exit_confirmed
        elif active_spmo_mode == "GATE":
            # GATE only gates ENTRY; exit still additionally drops out when
            # SPMO's own bull signal turns unconfirmed.
            drop = weak_exit or not spmo_bull_confirmed
        else:
            drop = weak_exit
        target_regime = sideways if drop else "ATTACK"
    elif state.regime == "WEAK" and use_weak:
        if attack_signal():
            target_regime = "ATTACK"
        elif weak_recover_ready:
            target_regime = "NEUTRAL"
        else:
            target_regime = "WEAK"
    else:
        target_regime = "ATTACK" if attack_signal() else sideways

    # The original only resets recovery_days (and bumps switch/entry/exit
    # counters) once an order for the new regime actually executes, gated on
    # every target instrument being tradable with price > 0. This backtest
    # assumes full price coverage for every listed ticker/proxy in range (no
    # trading halts), so a regime change always "executes" the same day.
    if target_regime != old_regime:
        state.recovery_days = 0
    state.regime = target_regime
    return target_regime, sig


def effective_targets(cfg, regime, today, role_weights_override=None):
    """Map desired roles into actually listed ETFs or transparent proxies.

    Returns (targets, proxy_weight) where targets is {ticker: weight} (listed
    tickers already merged if two roles share the same proxy), and
    proxy_weight is the fraction of the regime's weight currently routed
    through a substitute instrument.

    role_weights_override: if given, replaces cfg["ROLE_WEIGHTS"][regime] as
    the role->weight table before proxy routing (used by TILT to substitute
    its SPMO-trend-adjusted GPIQ/SPMO split for ATTACK's fixed weights)."""
    targets = {}
    proxy_weight = 0.0
    today = today if isinstance(today, date) else today.date()
    role_weights = role_weights_override if role_weights_override is not None else cfg["ROLE_WEIGHTS"][regime]
    for role, weight in role_weights.items():
        listed_from = cfg["ACTUAL_AVAILABLE_FROM"].get(role)
        listed = listed_from is None or today >= date(*listed_from)
        ticker = role if listed else cfg["PROXY_TICKER"][role]
        if not listed:
            proxy_weight += weight
        targets[ticker] = targets.get(ticker, 0.0) + weight
    return targets, proxy_weight


def entry_ok(regime, close, ma20, ret1, ret5):
    """Intraday opportunistic-DCA gate: same-regime idle cash can be put to
    work between rebalances. Ported from _entry_ok()."""
    if regime == "ATTACK":
        return close > ma20 and ret5 > 0
    if regime in ("NEUTRAL", "RECOVERY", "EARLY_RECOVERY"):
        return close >= ma20 or ret5 > 0
    if regime == "WEAK":
        return ret1 > 0
    if regime == "BEAR":
        return ret5 > 0
    return False

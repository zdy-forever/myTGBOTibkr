"""Unit/simulation tests against the SGS_V4C_Agent_开发交接.md section 6
checklist: state priority, short-term bounce while MA200 still weak, repeated
early-recovery triggers, WEAK exit, proxy ETF mapping, and weights summing to
98%. These stand in for the un-delivered reference/test_sgs_v4.py.
"""
import numpy as np
import pytest

from sgs import state_machine as sm
from sgs.config import BASE_CFG, ROLE_WEIGHT_SCHEMES, build_cfg


def make_prices(n=260, start=100.0, daily_return=0.0003, noise=0.0):
    rng = np.random.default_rng(0)
    returns = np.full(n, daily_return) + (rng.normal(0, noise, n) if noise else 0.0)
    return start * np.exp(np.cumsum(returns))


def test_role_weights_sum_to_98_percent():
    for scheme_name, role_weights in ROLE_WEIGHT_SCHEMES.items():
        for regime_name, weights in role_weights.items():
            assert abs(sum(weights.values()) - 0.98) < 1e-9, (scheme_name, regime_name)


def test_build_cfg_rejects_bad_experiment_or_mode():
    with pytest.raises(ValueError):
        build_cfg("NOT_A_MODE", "DEV_EARLY")
    with pytest.raises(ValueError):
        build_cfg("BASE", "NOT_A_PERIOD")


def test_final_holdout_locked_by_default():
    with pytest.raises(ValueError, match="LOCKED"):
        build_cfg("BASE", "FINAL_HOLDOUT")
    with pytest.raises(ValueError, match="LOCKED"):
        build_cfg("BASE", "FINAL_HOLDOUT", parameters_frozen=True, unlock_final_holdout=False)
    with pytest.raises(ValueError, match="LOCKED"):
        build_cfg("BASE", "FINAL_HOLDOUT", parameters_frozen=False, unlock_final_holdout=True)
    cfg = build_cfg("BASE", "FINAL_HOLDOUT", parameters_frozen=True, unlock_final_holdout=True)
    assert cfg["RUN_MODE"] == "FINAL_HOLDOUT"


def test_regime_priority_bear_overrides_everything_except_active_early_recovery():
    """A BASE-experiment state machine (use_early=False) must flip straight to
    BEAR the moment the bear condition is true, regardless of prior regime."""
    cfg = build_cfg("BASE", "DEV_EARLY")
    state = sm.StreakState(regime="ATTACK")
    prices = make_prices(260, start=100.0, daily_return=0.0)
    # Force a hard drawdown: fabricate the tail as a steep plunge.
    plunge = prices[-1] * np.exp(np.cumsum(np.full(10, -0.03)))
    series = np.r_[prices[:-10], plunge]
    regime, sig = sm.step(cfg, state, series)
    assert sig["dd126"] <= cfg["BEAR_DRAWDOWN_126"] or sig["ratio"] < cfg["BEAR_MA200_RATIO"]
    assert regime == "BEAR"


def _rally_then_decline(flat_before, rally_days, rally_pct, decline_days, decline_pct):
    """A synthetic series that rallies (so MA200 sits well above the eventual
    trough) then declines sharply -- used to put QQQ simultaneously "below its
    own MA200" and "freshly bounced off a 20-day low", matching the Jan-2016 /
    2018-Q4-style setup the EARLY_RECOVERY rule is meant to catch."""
    pre = np.full(flat_before, 100.0)
    rally = 100.0 * np.exp(np.linspace(0, np.log(1 + rally_pct), rally_days))
    decline = rally[-1] * np.exp(np.linspace(0, np.log(1 - decline_pct), decline_days))
    return np.r_[pre, rally, decline]


def test_early_recovery_overrides_bear_ma200_flag_when_use_early():
    """md section 4.1 / 使用说明.md section 1: a confirmed short-term bounce
    can move BEAR -> EARLY_RECOVERY even while price is STILL below MA200 --
    the existing bear flag must not immediately cancel it on the next day."""
    cfg = build_cfg("EARLY_ONLY", "DEV_EARLY")
    base = _rally_then_decline(50, 150, 0.30, 20, 0.30)
    bounce = base[-1] * np.exp(np.cumsum(np.full(10, 0.02)))
    series = np.r_[base, bounce]
    assert series[-1] < np.mean(series[-200:])  # still below MA200: true bear territory

    state = sm.StreakState(regime="BEAR")
    regime, sig = None, None
    for i in range(1, len(bounce) + 1):
        regime, sig = sm.step(cfg, state, series[:len(base) + i])
    assert regime == "EARLY_RECOVERY"
    assert sig["ratio"] < 1.0  # confirms it entered while still below MA200


def test_early_recovery_can_re_trigger_bear_on_panic_drop():
    cfg = build_cfg("EARLY_ONLY", "DEV_EARLY")
    state = sm.StreakState(regime="EARLY_RECOVERY")
    base = make_prices(250, start=100.0, daily_return=-0.0005)
    panic = base[-1] * np.exp(np.cumsum(np.full(5, -0.03)))
    series = np.r_[base, panic]
    regime, sig = sm.step(cfg, state, series)
    assert sig["ret5"] <= cfg["EARLY_PANIC_RETURN_5"]
    assert regime == "BEAR"


def test_weak_requires_two_day_confirmation_before_exiting_to_neutral():
    """md 使用说明.md section 1 (V4-B): WEAK -> NEUTRAL requires QQQ to close
    above MA20 with positive 5-day return for WEAK_RECOVER_CONFIRM_DAYS
    consecutive days, not just the first qualifying day."""
    cfg = build_cfg("WEAK_ONLY", "DEV_EARLY")
    assert cfg["WEAK_RECOVER_CONFIRM_DAYS"] == 2
    base = _rally_then_decline(50, 150, 0.25, 20, 0.12)
    sig = sm.signals(cfg, base)
    assert sig["close"] < sig["ma50"] and sig["ret63"] < cfg["WEAK_RETURN_63"] and sig["dd126"] <= cfg["WEAK_DRAWDOWN_126"]

    rebound = base[-1] * np.exp(np.cumsum(np.full(4, 0.02)))
    series = np.r_[base, rebound]
    state = sm.StreakState(regime="WEAK")
    regimes = [sm.step(cfg, state, series[: len(base) + i])[0] for i in range(1, 5)]
    # Day 1 of the qualifying streak restarts/continues confirmation; day 2's
    # DATA (evaluated as the 3rd call here) flips the streak counter to 2 and
    # the transition fires -- the first call must NOT already be NEUTRAL.
    assert regimes[0] == "WEAK"
    assert "NEUTRAL" in regimes
    assert regimes.index("NEUTRAL") >= 1  # never fires on the very first qualifying day


def test_weak_recovery_without_use_weak_keeps_base_behavior():
    """BASE experiment has no WEAK regime transition rule of its own in this
    simplified state machine port; WEAK only occurs via the generic
    'sideways' fallback and is immediately re-evaluated like any other
    non-WEAK/non-RECOVERY/non-ATTACK/non-BEAR state."""
    cfg = build_cfg("BASE", "DEV_EARLY")
    assert cfg["USE_WEAK"] is False
    state = sm.StreakState(regime="WEAK")
    prices = make_prices(220, start=100.0, daily_return=0.002)
    regime, sig = sm.step(cfg, state, prices)
    # Since USE_WEAK is False, falling through to the final else branch:
    # ATTACK if bull else sideways(weak(...)).
    assert regime in ("ATTACK", "WEAK", "NEUTRAL")


def test_effective_targets_routes_to_proxy_before_listing_date():
    cfg = build_cfg("BASE", "DEV_EARLY")
    from datetime import date
    targets, proxy_weight = sm.effective_targets(cfg, "ATTACK", date(2016, 6, 1))
    # GPIQ not listed until 2023-11; SPMO is listed from 2015-11.
    assert targets == {"QQQ": 0.40, "SPMO": 0.58}
    assert abs(proxy_weight - 0.40) < 1e-9


def test_effective_targets_uses_real_ticker_after_listing_date():
    cfg = build_cfg("BASE", "DEV_RECENT")
    from datetime import date
    targets, proxy_weight = sm.effective_targets(cfg, "ATTACK", date(2024, 1, 1))
    assert targets == {"GPIQ": 0.40, "SPMO": 0.58}
    assert proxy_weight == 0.0


def test_effective_targets_merges_shared_proxy_weight():
    """BEAR = SGOV 0.78 + JEPI 0.20. Before SGOV/JEPI both exist (pre 2020-06),
    SGOV->BIL and JEPI->SPLV map to DIFFERENT proxies, so no merge happens
    here -- but the BEAR regime before 2020-06-01 should route cleanly."""
    cfg = build_cfg("BASE", "DEV_EARLY")
    from datetime import date
    targets, proxy_weight = sm.effective_targets(cfg, "BEAR", date(2016, 6, 1))
    assert targets == {"BIL": 0.78, "SPLV": 0.20}
    assert abs(proxy_weight - 0.98) < 1e-9


def test_entry_ok_gates_match_reference():
    assert sm.entry_ok("ATTACK", close=101, ma20=100, ret1=0.0, ret5=0.01) is True
    assert sm.entry_ok("ATTACK", close=99, ma20=100, ret1=0.0, ret5=0.01) is False
    assert sm.entry_ok("BEAR", close=100, ma20=100, ret1=0.0, ret5=0.01) is True
    assert sm.entry_ok("BEAR", close=100, ma20=100, ret1=0.0, ret5=-0.01) is False
    assert sm.entry_ok("WEAK", close=100, ma20=100, ret1=0.01, ret5=-0.01) is True
    assert sm.entry_ok("UNKNOWN", close=100, ma20=100, ret1=1, ret5=1) is False


def test_insufficient_history_returns_none():
    cfg = build_cfg("BASE", "DEV_EARLY")
    state = sm.StreakState()
    regime, sig = sm.step(cfg, state, np.ones(50))
    assert regime is None and sig is None


# ---------------------------------------------------------------- SPMO trend-signal experiment

def test_trend_source_spmo_rejects_spmo_mode():
    with pytest.raises(ValueError, match="cannot be both"):
        build_cfg("BASE", "DEV_EARLY", trend_source="SPMO", spmo_mode="GATE")
    with pytest.raises(ValueError, match="cannot be both"):
        build_cfg("BASE", "DEV_EARLY", trend_source="SPMO", spmo_mode="TILT")


def test_build_cfg_defaults_are_backward_compatible():
    cfg = build_cfg("BASE", "DEV_EARLY")
    assert cfg["TREND_SOURCE"] == "QQQ"
    assert cfg["SPMO_MODE"] is None


def test_spmo_bull_signal_needs_two_day_confirmation():
    cfg = build_cfg("BASE", "DEV_EARLY")
    state = sm.StreakState()
    prices = make_prices(220, start=100.0, daily_return=0.003)
    confirmed1, sig1 = sm.spmo_bull_signal(cfg, state, prices)
    assert sig1 is not None
    assert confirmed1 is False  # streak == 1, CONFIRM_DAYS == 2
    confirmed2, sig2 = sm.spmo_bull_signal(cfg, state, prices)
    assert confirmed2 is True  # streak == 2


def test_spmo_bull_signal_insufficient_history_returns_none():
    cfg = build_cfg("BASE", "DEV_EARLY")
    state = sm.StreakState()
    confirmed, sig = sm.spmo_bull_signal(cfg, state, np.ones(50))
    assert confirmed is None and sig is None


def test_spmo_exit_signal_needs_two_day_confirmation():
    cfg = build_cfg("BASE", "DEV_EARLY")
    state = sm.StreakState()
    base = make_prices(240, start=100.0, daily_return=0.0)
    plunge = base[-1] * np.exp(np.cumsum(np.full(10, -0.03)))
    series = np.r_[base[:-10], plunge]
    confirmed1, sig1 = sm.spmo_exit_signal(cfg, state, series)
    assert sig1 is not None
    assert confirmed1 is False  # streak == 1, CONFIRM_DAYS == 2
    confirmed2, sig2 = sm.spmo_exit_signal(cfg, state, series)
    assert confirmed2 is True  # streak == 2


def test_spmo_exit_signal_insufficient_history_returns_none():
    cfg = build_cfg("BASE", "DEV_EARLY")
    state = sm.StreakState()
    confirmed, sig = sm.spmo_exit_signal(cfg, state, np.ones(50))
    assert confirmed is None and sig is None


def test_spmo_exit_signal_false_when_no_exit_condition_true():
    cfg = build_cfg("BASE", "DEV_EARLY")
    state = sm.StreakState()
    prices = make_prices(220, start=100.0, daily_return=0.002)  # steadily rising
    confirmed, sig = sm.spmo_exit_signal(cfg, state, prices)
    assert confirmed is False


def test_tilt_attack_weights_bounds_and_sum():
    cfg = build_cfg("BASE", "DEV_EARLY")
    role = cfg["ROLE_WEIGHTS"]["ATTACK"]
    total = role["GPIQ"] + role["SPMO"]

    strong = dict(ratio=cfg["BULL_MA200_RATIO"] + 0.05)
    tilted_strong = sm.tilt_attack_weights(cfg, role, strong)
    assert tilted_strong["SPMO"] == pytest.approx(role["SPMO"])
    assert tilted_strong["GPIQ"] == pytest.approx(role["GPIQ"])

    weak = dict(ratio=cfg["BEAR_MA200_RATIO"] - 0.05)
    tilted_weak = sm.tilt_attack_weights(cfg, role, weak)
    assert tilted_weak["SPMO"] == pytest.approx(cfg["SPMO_TILT_MIN_WEIGHT"])

    for ratio in np.linspace(cfg["BEAR_MA200_RATIO"] - 0.1, cfg["BULL_MA200_RATIO"] + 0.1, 25):
        tilted = sm.tilt_attack_weights(cfg, role, dict(ratio=ratio))
        assert tilted["GPIQ"] + tilted["SPMO"] == pytest.approx(total)
        assert cfg["SPMO_TILT_MIN_WEIGHT"] - 1e-9 <= tilted["SPMO"] <= role["SPMO"] + 1e-9

    # Monotonic in ratio.
    ratios = np.linspace(cfg["BEAR_MA200_RATIO"], cfg["BULL_MA200_RATIO"], 10)
    spmo_weights = [sm.tilt_attack_weights(cfg, role, dict(ratio=r))["SPMO"] for r in ratios]
    assert all(b >= a - 1e-9 for a, b in zip(spmo_weights, spmo_weights[1:]))


def test_gate_blocks_attack_when_spmo_not_confirmed_even_if_qqq_is_bullish():
    cfg = build_cfg("BASE", "DEV_EARLY", spmo_mode="GATE")
    state = sm.StreakState(regime="NEUTRAL")
    qqq_prices = make_prices(260, start=100.0, daily_return=0.004)  # clearly bullish QQQ
    regime, sig = sm.step(cfg, state, qqq_prices, spmo_bull_confirmed=False)
    assert sig["ratio"] > cfg["BULL_MA200_RATIO"]  # QQQ bull condition is true
    assert regime != "ATTACK"  # but SPMO's own veto blocks it


def test_gate_allows_attack_when_both_confirm():
    cfg = build_cfg("BASE", "DEV_EARLY", spmo_mode="GATE")
    state = sm.StreakState(regime="NEUTRAL")
    qqq_prices = make_prices(260, start=100.0, daily_return=0.004)
    sm.step(cfg, state, qqq_prices, spmo_bull_confirmed=True)  # build QQQ's own bull streak to CONFIRM_DAYS
    regime, sig = sm.step(cfg, state, qqq_prices, spmo_bull_confirmed=True)
    assert regime == "ATTACK"


def test_dual_enters_attack_on_spmo_alone_even_if_qqq_is_not_bullish():
    cfg = build_cfg("BASE", "DEV_EARLY", spmo_mode="DUAL")
    state = sm.StreakState(regime="NEUTRAL")
    flat_qqq = make_prices(220, start=100.0, daily_return=0.0)  # QQQ bull condition false
    regime, sig = sm.step(cfg, state, flat_qqq, spmo_bull_confirmed=True)
    assert sig["ratio"] <= cfg["BULL_MA200_RATIO"]  # confirm QQQ itself is not bullish
    assert regime == "ATTACK"  # SPMO's own confirmation is enough under DUAL


def test_gate_drops_out_of_attack_when_spmo_bull_turns_unconfirmed():
    flat_qqq = make_prices(220, start=100.0, daily_return=0.0005)  # not weak_exit on its own
    cfg = build_cfg("BASE", "DEV_EARLY", spmo_mode="GATE")
    state = sm.StreakState(regime="ATTACK")
    regime, sig = sm.step(cfg, state, flat_qqq, spmo_bull_confirmed=False)
    assert regime != "ATTACK"


def test_dual_exit_ignores_qqq_weak_exit_and_uses_spmo_exit_only():
    """DUAL's ATTACK exit must be judged ENTIRELY on spmo_exit_confirmed, not
    mixed with QQQ's weak_exit -- this is the fix for the whipsaw found in
    the DEV_RECENT 2026-07 state audit (QQQ chopping around its exit
    thresholds while SPMO was still confirmed strong caused daily flips)."""
    flat_qqq = make_prices(220, start=100.0, daily_return=0.0005)  # QQQ's own weak_exit stays False
    cfg = build_cfg("BASE", "DEV_EARLY", spmo_mode="DUAL")
    state = sm.StreakState(regime="ATTACK")

    # SPMO exit NOT confirmed: stays in ATTACK even if spmo_bull_confirmed is False
    # (bull/exit are independent signals; only spmo_exit_confirmed governs the drop).
    regime, _ = sm.step(cfg, state, flat_qqq, spmo_bull_confirmed=False, spmo_exit_confirmed=False)
    assert regime == "ATTACK"

    # SPMO exit confirmed: drops out, even though QQQ's own weak_exit is False.
    state2 = sm.StreakState(regime="ATTACK")
    regime2, sig2 = sm.step(cfg, state2, flat_qqq, spmo_bull_confirmed=True, spmo_exit_confirmed=True)
    assert sig2["close"] >= cfg["EXIT_MA50_RATIO"] * sig2["ma50"] and sig2["ret63"] >= cfg["EXIT_RETURN_63"]  # QQQ weak_exit is False
    assert regime2 != "ATTACK"


def test_dual_exit_falls_back_to_qqq_weak_exit_when_spmo_history_insufficient():
    cfg = build_cfg("BASE", "DEV_EARLY", spmo_mode="DUAL")
    state = sm.StreakState(regime="ATTACK")
    plunge = make_prices(200, start=100.0, daily_return=0.0) * 0  # force weak_exit via degenerate drop
    # Use a real plunge so QQQ's own weak_exit condition is genuinely true.
    base = make_prices(240, start=100.0, daily_return=0.0)
    plunge_tail = base[-1] * np.exp(np.cumsum(np.full(10, -0.03)))
    series = np.r_[base[:-10], plunge_tail]
    regime, sig = sm.step(cfg, state, series, spmo_bull_confirmed=None, spmo_exit_confirmed=None)
    assert regime != "ATTACK"  # falls back to QQQ's weak_exit when SPMO's own exit is unavailable


def test_spmo_mode_inert_when_confirmation_unavailable():
    """spmo_bull_confirmed=None (SPMO history not long enough yet) must fall
    back to plain QQQ-only behavior, matching a run with SPMO_MODE=None."""
    cfg_gate = build_cfg("BASE", "DEV_EARLY", spmo_mode="GATE")
    cfg_base = build_cfg("BASE", "DEV_EARLY")
    qqq_prices = make_prices(260, start=100.0, daily_return=0.004)
    state_gate = sm.StreakState(regime="NEUTRAL")
    state_base = sm.StreakState(regime="NEUTRAL")
    sm.step(cfg_gate, state_gate, qqq_prices, spmo_bull_confirmed=None)  # build QQQ bull streak to CONFIRM_DAYS
    sm.step(cfg_base, state_base, qqq_prices)
    regime_gate, _ = sm.step(cfg_gate, state_gate, qqq_prices, spmo_bull_confirmed=None)
    regime_base, _ = sm.step(cfg_base, state_base, qqq_prices)
    assert regime_gate == regime_base == "ATTACK"


# ---------------------------------------------------------------- consolidated weight-scheme experiment

def test_build_cfg_rejects_unknown_weight_scheme():
    with pytest.raises(ValueError, match="WEIGHT_SCHEME"):
        build_cfg("BASE", "DEV_EARLY", weight_scheme="BOGUS")


def test_build_cfg_rejects_tilt_with_consolidated_scheme():
    """CONSOLIDATED's ATTACK has only SPMO, no GPIQ; TILT's interpolation
    needs both. This must be refused at config time, not surfaced as a
    KeyError deep inside tilt_attack_weights() during a backtest."""
    with pytest.raises(ValueError, match="TILT"):
        build_cfg("BASE", "DEV_EARLY", spmo_mode="TILT", weight_scheme="CONSOLIDATED")


def test_consolidated_scheme_defaults_are_backward_compatible():
    cfg = build_cfg("BASE", "DEV_EARLY")
    assert cfg["WEIGHT_SCHEME"] == "ORIGINAL"
    assert cfg["ROLE_WEIGHTS"]["ATTACK"] == {"GPIQ": 0.40, "SPMO": 0.58}


def test_consolidated_scheme_shares_identical_weights_across_attack_neutral_recovery():
    """This is the load-bearing assumption behind the whole experiment: if
    ATTACK/NEUTRAL/RECOVERY don't share an IDENTICAL target weight table,
    switching between them still liquidates/rebuilds the SPMO position and
    the churn-reduction premise doesn't hold."""
    cfg = build_cfg("BASE", "DEV_EARLY", weight_scheme="CONSOLIDATED")
    assert cfg["ROLE_WEIGHTS"]["ATTACK"] == cfg["ROLE_WEIGHTS"]["NEUTRAL"] == cfg["ROLE_WEIGHTS"]["RECOVERY"]
    assert cfg["ROLE_WEIGHTS"]["ATTACK"] == {"SPMO": 0.98}


def test_consolidated_scheme_removes_gpiq_everywhere():
    cfg = build_cfg("BASE", "DEV_EARLY", weight_scheme="CONSOLIDATED")
    for regime, weights in cfg["ROLE_WEIGHTS"].items():
        assert "GPIQ" not in weights, regime


def test_consolidated_scheme_keeps_jepi_only_in_bear():
    cfg = build_cfg("BASE", "DEV_EARLY", weight_scheme="CONSOLIDATED")
    for regime, weights in cfg["ROLE_WEIGHTS"].items():
        if regime == "BEAR":
            assert weights.get("JEPI") == pytest.approx(0.20)
        else:
            assert "JEPI" not in weights, regime


def test_consolidated_scheme_weak_and_early_recovery_are_mirrored_60_40():
    cfg = build_cfg("BASE", "DEV_EARLY", weight_scheme="CONSOLIDATED")
    weak, early = cfg["ROLE_WEIGHTS"]["WEAK"], cfg["ROLE_WEIGHTS"]["EARLY_RECOVERY"]
    assert weak["SGOV"] == pytest.approx(0.98 * 0.6)
    assert weak["SPMO"] == pytest.approx(0.98 * 0.4)
    assert early["SPMO"] == pytest.approx(0.98 * 0.6)
    assert early["SGOV"] == pytest.approx(0.98 * 0.4)


def test_effective_targets_with_consolidated_scheme_routes_proxies_correctly():
    from datetime import date
    cfg_early = build_cfg("BASE", "DEV_EARLY", weight_scheme="CONSOLIDATED")
    # SPMO itself is listed from 2015-11, so 2016-06 uses the real ticker.
    targets, proxy_weight = sm.effective_targets(cfg_early, "ATTACK", date(2016, 6, 1))
    assert targets == {"SPMO": 0.98}
    assert proxy_weight == 0.0

    # BEAR is unchanged from ORIGINAL: before SGOV/JEPI exist (pre 2020-06),
    # both route through their proxies.
    targets_bear, proxy_weight_bear = sm.effective_targets(cfg_early, "BEAR", date(2016, 6, 1))
    assert targets_bear == {"BIL": 0.78, "SPLV": 0.20}
    assert abs(proxy_weight_bear - 0.98) < 1e-9

    cfg_recent = build_cfg("BASE", "DEV_RECENT", weight_scheme="CONSOLIDATED")
    targets_bear2, proxy_weight_bear2 = sm.effective_targets(cfg_recent, "BEAR", date(2024, 1, 1))
    assert targets_bear2 == {"SGOV": 0.78, "JEPI": 0.20}
    assert proxy_weight_bear2 == 0.0


# ---------------------------------------------------------------- BEAR entry/recovery logic experiment

def test_build_cfg_rejects_unknown_bear_logic():
    with pytest.raises(ValueError, match="BEAR_LOGIC"):
        build_cfg("BASE", "DEV_EARLY", bear_logic="BOGUS")


def test_bear_logic_defaults_are_backward_compatible():
    cfg = build_cfg("BASE", "DEV_EARLY")
    assert cfg["BEAR_LOGIC"] == "ORIGINAL"


def _crash_then_recover(flat_days, decline_days, decline_pct, recover_days, recover_pct):
    """A synthetic series: flat -> sharp decline -> partial recovery, used to
    probe BEAR entry/recovery timing without real market data."""
    flat = np.full(flat_days, 100.0)
    decline = flat[-1] * np.exp(np.linspace(0, np.log(1 - decline_pct), decline_days))
    recover = decline[-1] * np.exp(np.linspace(0, np.log(1 + recover_pct), recover_days))
    return np.r_[flat, decline, recover]


def test_bear_logic_original_is_byte_identical_for_entry_and_recovery():
    """Building the exact same synthetic crash/recovery series and stepping
    through it day by day must produce IDENTICAL regime sequences whether
    bear_logic is passed explicitly as "ORIGINAL" or omitted (default)."""
    series = _crash_then_recover(220, 15, 0.20, 40, 0.25)
    cfg_default = build_cfg("BASE", "DEV_EARLY")
    cfg_explicit = build_cfg("BASE", "DEV_EARLY", bear_logic="ORIGINAL")
    state_a, state_b = sm.StreakState(regime="ATTACK"), sm.StreakState(regime="ATTACK")
    for i in range(201, len(series) + 1):
        ra, _ = sm.step(cfg_default, state_a, series[:i])
        rb, _ = sm.step(cfg_explicit, state_b, series[:i])
        assert ra == rb


def test_bear_entry_is_identical_across_all_bear_logic_values():
    """BEAR entry must be byte-identical across every registered BEAR_LOGIC
    value (currently ORIGINAL and RECOVERY_ONLY_IMPROVED) and across a range
    of crash speeds -- a prototype that gated entry behind a same-day dd126
    depth floor was found (via this exact kind of sweep) to delay entry more
    the SLOWER a decline was, up to never confirming at all for a genuinely
    slow ~-10% decline, so entry must never vary by BEAR_LOGIC at all."""
    scenarios = [(5, 0.15), (10, 0.20), (10, 0.25), (15, 0.20), (20, 0.20), (20, 0.10)]
    for decline_days, decline_pct in scenarios:
        series = _crash_then_recover(220, decline_days, decline_pct, 5, 0.05)
        cfg_original = build_cfg("BASE", "DEV_EARLY")
        cfg_improved = build_cfg("BASE", "DEV_EARLY", bear_logic="RECOVERY_ONLY_IMPROVED")
        state_o, state_i = sm.StreakState(regime="ATTACK"), sm.StreakState(regime="ATTACK")
        for i in range(201, len(series) + 1):
            ro, _ = sm.step(cfg_original, state_o, series[:i])
            ri, _ = sm.step(cfg_improved, state_i, series[:i])
            assert ro == ri, (decline_days, decline_pct, i)


def test_recovery_only_improved_leaves_bear_entry_untouched():
    """RECOVERY_ONLY_IMPROVED must produce the IDENTICAL BEAR-entry sequence
    as ORIGINAL (only the recovering condition differs)."""
    series = _crash_then_recover(220, 15, 0.20, 40, 0.25)
    cfg_original = build_cfg("BASE", "DEV_EARLY")
    cfg_recovery_only = build_cfg("BASE", "DEV_EARLY", bear_logic="RECOVERY_ONLY_IMPROVED")
    state_o, state_r = sm.StreakState(regime="ATTACK"), sm.StreakState(regime="ATTACK")
    for i in range(201, len(series) + 1):
        ro, _ = sm.step(cfg_original, state_o, series[:i])
        rr, _ = sm.step(cfg_recovery_only, state_r, series[:i])
        if ro == "BEAR" or rr == "BEAR":
            assert ro == rr == "BEAR"


def test_recovery_only_improved_can_exit_bear_no_later_than_original():
    """RECOVERY_ONLY_IMPROVED adds an independent recovering path (ret63 back
    positive + ratio>1.0) alongside ORIGINAL's -- so recovery should never
    take LONGER than ORIGINAL, only ever the same or faster."""
    series = _crash_then_recover(220, 15, 0.20, 60, 0.30)
    cfg_original = build_cfg("BASE", "DEV_EARLY")
    cfg_improved = build_cfg("BASE", "DEV_EARLY", bear_logic="RECOVERY_ONLY_IMPROVED")
    state_o, state_i = sm.StreakState(regime="BEAR"), sm.StreakState(regime="BEAR")
    exit_day_o = exit_day_i = None
    for i in range(201, len(series) + 1):
        ro, _ = sm.step(cfg_original, state_o, series[:i])
        ri, _ = sm.step(cfg_improved, state_i, series[:i])
        if exit_day_o is None and ro != "BEAR":
            exit_day_o = i
        if exit_day_i is None and ri != "BEAR":
            exit_day_i = i
        if exit_day_o is not None and exit_day_i is not None:
            break
    assert exit_day_i is not None and exit_day_o is not None
    assert exit_day_i <= exit_day_o


# ---------------------------------------------------------------- ATTACK/NEUTRAL/RECOVERY held-asset experiment

def test_build_cfg_rejects_unknown_attack_asset():
    with pytest.raises(ValueError, match="ATTACK_ASSET"):
        build_cfg("BASE", "DEV_EARLY", attack_asset="BOGUS")
    with pytest.raises(ValueError, match="ATTACK_ASSET_COMPARISON"):
        build_cfg("BASE", "DEV_EARLY", attack_asset_comparison="BOGUS")


def test_attack_asset_defaults_are_backward_compatible():
    cfg = build_cfg("BASE", "DEV_EARLY")
    assert cfg["ATTACK_ASSET"] == "SPMO"
    assert cfg["ATTACK_ASSET_COMPARISON"] == "RATIO"


def test_stronger_asset_picks_qqq_when_qqq_ratio_is_higher():
    cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER", attack_asset_comparison="RATIO")
    qqq_prices = make_prices(220, start=100.0, daily_return=0.004)  # strongly rising
    spmo_prices = make_prices(220, start=100.0, daily_return=0.0005)  # flat
    winner, qsig, ssig = sm.stronger_asset(cfg, qqq_prices, spmo_prices)
    assert winner == "QQQ"
    assert qsig["ratio"] > ssig["ratio"]


def test_stronger_asset_picks_spmo_when_spmo_ratio_is_higher():
    cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER", attack_asset_comparison="RATIO")
    qqq_prices = make_prices(220, start=100.0, daily_return=0.0005)  # flat
    spmo_prices = make_prices(220, start=100.0, daily_return=0.004)  # strongly rising
    winner, qsig, ssig = sm.stronger_asset(cfg, qqq_prices, spmo_prices)
    assert winner == "SPMO"
    assert ssig["ratio"] > qsig["ratio"]


def test_stronger_asset_ret20_comparison_picks_correctly():
    cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER", attack_asset_comparison="RET20")
    qqq_prices = make_prices(220, start=100.0, daily_return=0.004)
    spmo_prices = make_prices(220, start=100.0, daily_return=0.0005)
    winner, qsig, ssig = sm.stronger_asset(cfg, qqq_prices, spmo_prices)
    assert winner == "QQQ"
    assert qsig["ret20"] > ssig["ret20"]


def test_stronger_asset_insufficient_history_returns_none():
    cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER")
    winner, qsig, ssig = sm.stronger_asset(cfg, np.ones(50), np.ones(220))
    assert winner is None and qsig is None and ssig is None
    winner2, qsig2, ssig2 = sm.stronger_asset(cfg, np.ones(220), np.ones(50))
    assert winner2 is None and qsig2 is None and ssig2 is None


# ---------------------------------------------------------------- ASSET_SWITCH_MODE (whipsaw suppression)

def test_build_cfg_rejects_unknown_asset_switch_mode():
    with pytest.raises(ValueError, match="ASSET_SWITCH_MODE"):
        build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER", asset_switch_mode="BOGUS")


def test_build_cfg_rejects_non_daily_switch_mode_without_stronger_asset():
    with pytest.raises(ValueError, match="asset_switch_mode"):
        build_cfg("BASE", "DEV_EARLY", attack_asset="SPMO", asset_switch_mode="CONFIRM_N")
    with pytest.raises(ValueError, match="asset_switch_mode"):
        build_cfg("BASE", "DEV_EARLY", attack_asset="QQQ", asset_switch_mode="LOCK_PER_EPISODE")


def test_asset_switch_mode_default_is_backward_compatible():
    cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER")
    assert cfg["ASSET_SWITCH_MODE"] == "DAILY"


def test_resolve_attack_asset_daily_mode_matches_stronger_asset_directly():
    """DAILY must be byte-identical to calling stronger_asset() directly --
    this is the regression check that the default behavior is unchanged."""
    cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER", asset_switch_mode="DAILY")
    qqq_prices = make_prices(220, start=100.0, daily_return=0.004)
    spmo_prices = make_prices(220, start=100.0, daily_return=0.0005)
    state = sm.StreakState(regime="ATTACK")
    winner, qsig, ssig = sm.resolve_attack_asset(cfg, state, "ATTACK", "ATTACK", qqq_prices, spmo_prices)
    expected_winner, eqsig, essig = sm.stronger_asset(cfg, qqq_prices, spmo_prices)
    assert winner == expected_winner == "QQQ"
    assert qsig == eqsig and ssig == essig
    assert state.held_asset == winner


def test_resolve_attack_asset_insufficient_history_leaves_held_asset_untouched():
    for mode in ("DAILY", "CONFIRM_N", "MARGIN_BAND"):
        cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER", asset_switch_mode=mode)
        state = sm.StreakState(regime="ATTACK", held_asset="SPMO")
        winner, qsig, ssig = sm.resolve_attack_asset(cfg, state, "ATTACK", "ATTACK", np.ones(50), np.ones(220))
        assert winner is None and qsig is None and ssig is None, mode
        assert state.held_asset == "SPMO", mode

    # LOCK_PER_EPISODE: insufficient history right on entry must not crash and
    # must leave held_asset at its (unset) default rather than a bogus value.
    cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER", asset_switch_mode="LOCK_PER_EPISODE")
    state = sm.StreakState(held_asset=None)
    winner, qsig, ssig = sm.resolve_attack_asset(cfg, state, "ATTACK", "WEAK", np.ones(50), np.ones(220))
    assert winner is None and qsig is None and ssig is None
    assert state.held_asset is None


def test_confirm_n_requires_consecutive_leads_before_switching(monkeypatch):
    """A new winner must lead for ASSET_SWITCH_CONFIRM_DAYS consecutive days
    before the held asset actually changes; a lead that breaks early resets
    the streak and the old holding is kept."""
    cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER", asset_switch_mode="CONFIRM_N")
    assert cfg["ASSET_SWITCH_CONFIRM_DAYS"] == 3
    state = sm.StreakState(regime="ATTACK", held_asset="SPMO")
    dummy_sig = {"ratio": 1.0, "ret20": 0.0}

    # QQQ leads for 2 days (not enough), then SPMO reclaims the lead (streak
    # resets), then QQQ leads for 3 consecutive days (enough) -> switches on
    # the 3rd.
    sequence = ["QQQ", "QQQ", "SPMO", "QQQ", "QQQ", "QQQ"]
    expected_held = ["SPMO", "SPMO", "SPMO", "SPMO", "SPMO", "QQQ"]
    for winner, expected in zip(sequence, expected_held):
        monkeypatch.setattr(sm, "stronger_asset", lambda cfg_, qqq, spmo, w=winner: (w, dummy_sig, dummy_sig))
        held, _, _ = sm.resolve_attack_asset(cfg, state, "ATTACK", "ATTACK", None, None)
        assert held == expected
        assert state.held_asset == expected


def test_margin_band_requires_minimum_relative_lead_before_switching(monkeypatch):
    cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER", asset_switch_mode="MARGIN_BAND",
                     attack_asset_comparison="RATIO")
    assert cfg["ASSET_SWITCH_MARGIN"] == 0.01
    state = sm.StreakState(regime="ATTACK", held_asset="SPMO")

    # QQQ nominally leads (stronger_asset's own tie-break would pick it), but
    # only by 0.5% -- below the 1% margin -- so MARGIN_BAND must hold SPMO.
    qsig_small, ssig_small = {"ratio": 1.005, "ret20": 0.0}, {"ratio": 1.0, "ret20": 0.0}
    monkeypatch.setattr(sm, "stronger_asset", lambda cfg_, qqq, spmo: ("QQQ", qsig_small, ssig_small))
    winner, _, _ = sm.resolve_attack_asset(cfg, state, "ATTACK", "ATTACK", None, None)
    assert winner == "SPMO"
    assert state.held_asset == "SPMO"

    # Now QQQ leads by 2% -- above the margin -- must switch.
    qsig_big, ssig_big = {"ratio": 1.02, "ret20": 0.0}, {"ratio": 1.0, "ret20": 0.0}
    monkeypatch.setattr(sm, "stronger_asset", lambda cfg_, qqq, spmo: ("QQQ", qsig_big, ssig_big))
    winner, _, _ = sm.resolve_attack_asset(cfg, state, "ATTACK", "ATTACK", None, None)
    assert winner == "QQQ"
    assert state.held_asset == "QQQ"


def test_margin_band_handles_negative_ret20_without_crashing(monkeypatch):
    """ret20 can be negative even in a risk-on regime in principle (mild
    confirmation thresholds); MARGIN_BAND's relative comparison must still
    produce a plain boolean decision, not raise or produce a NaN-poisoned
    comparison."""
    cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER", asset_switch_mode="MARGIN_BAND",
                     attack_asset_comparison="RET20")
    state = sm.StreakState(held_asset="SPMO")
    qsig, ssig = {"ratio": 1.0, "ret20": -0.01}, {"ratio": 1.0, "ret20": -0.05}
    monkeypatch.setattr(sm, "stronger_asset", lambda cfg_, qqq, spmo: ("QQQ", qsig, ssig))
    winner, _, _ = sm.resolve_attack_asset(cfg, state, "ATTACK", "ATTACK", None, None)
    assert winner in ("SPMO", "QQQ")


def test_lock_per_episode_holds_through_group_churn_and_rebinds_only_on_reentry(monkeypatch):
    """The held asset is decided once on entering the ATTACK/NEUTRAL/RECOVERY
    group from outside it, then must NOT change while churning between those
    three regimes, even if stronger_asset's own day-to-day pick would flip --
    only exiting the group and re-entering triggers a fresh decision."""
    cfg = build_cfg("BASE", "DEV_EARLY", attack_asset="STRONGER", asset_switch_mode="LOCK_PER_EPISODE")
    qqq_prices = make_prices(220, start=100.0, daily_return=0.001)
    spmo_prices = make_prices(220, start=100.0, daily_return=0.001)
    state = sm.StreakState()

    winners = iter(["SPMO", "QQQ"])  # only entering-the-group days call stronger_asset at all
    monkeypatch.setattr(sm, "stronger_asset",
                         lambda cfg_, qqq, spmo: (next(winners), {"ratio": 1.0, "ret20": 0.0}, {"ratio": 1.0, "ret20": 0.0}))

    # Enters ATTACK from WEAK (outside the group) -> decides once: SPMO.
    w, _, _ = sm.resolve_attack_asset(cfg, state, "ATTACK", "WEAK", qqq_prices, spmo_prices)
    assert w == "SPMO" and state.held_asset == "SPMO"

    # Stays inside the group through ATTACK -> NEUTRAL -> RECOVERY -> ATTACK;
    # each of these must keep the lock despite the (unused, since
    # stronger_asset isn't even called here) "QQQ" queued up next.
    for target, old in [("ATTACK", "ATTACK"), ("NEUTRAL", "ATTACK"), ("RECOVERY", "NEUTRAL"), ("ATTACK", "RECOVERY")]:
        w, _, _ = sm.resolve_attack_asset(cfg, state, target, old, qqq_prices, spmo_prices)
        assert w == "SPMO", (target, old)
        assert state.held_asset == "SPMO"

    # Exits the group (old regime RECOVERY, but we now re-enter from WEAK) ->
    # fresh decision: QQQ.
    w, _, _ = sm.resolve_attack_asset(cfg, state, "ATTACK", "WEAK", qqq_prices, spmo_prices)
    assert w == "QQQ" and state.held_asset == "QQQ"

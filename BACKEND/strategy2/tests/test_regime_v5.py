"""Unit tests for the V5 five-bucket continuous-allocation track
(regime_v5.py/config_v5.py) -- pure functions on synthetic price series, no
network. Mirrors test_state_machine.py's style (own make_prices() helper)
but is an entirely independent test file, since V5 is a parallel track with
no coupling to state_machine.py's StreakState/step().
"""
from datetime import date

import numpy as np
import pytest

from sgs import regime_v5 as rv5
from sgs.config_v5 import GPIQ_AVAILABLE_FROM, build_cfg_v5


def make_prices(n=260, start=100.0, daily_return=0.0003, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    returns = np.full(n, daily_return) + (rng.normal(0, noise, n) if noise else 0.0)
    return start * np.exp(np.cumsum(returns))


# ---------------------------------------------------------------- 5-bucket classification

def test_threshold_ext_classifies_strong_up_above_strict_ratio_and_ret63():
    cfg = build_cfg_v5("DEV_EARLY", classifier_mode="THRESHOLD_EXT")
    prices = make_prices(220, daily_return=0.004)  # strongly rising
    state = rv5.V5StreakState()
    for i in range(201, len(prices) + 1):
        bucket, sig = rv5.step_v5(cfg, state, prices[:i])
    assert sig["ratio"] >= cfg["V5_STRONG_UP_RATIO"]
    assert bucket == "STRONG_UP"


def test_threshold_ext_classifies_plain_up_between_up_and_strong_up_ratio():
    # The THRESHOLD strong-up rule and the original 1.03 UP entry are pinned:
    # under the default EFFICIENCY rule a smooth synthetic rise is "strong".
    cfg = build_cfg_v5("DEV_EARLY", classifier_mode="THRESHOLD_EXT", strong_up_rule="THRESHOLD",
                       overrides={"V5_UP_RATIO": 1.03})
    # 0.0004 (not 0.0006): V5_STRONG_UP_RATIO was narrowed from 1.08 to 1.06
    # by the sensitivity sweep (see config_v5.py's comment), so the old
    # daily_return no longer reliably lands strictly below the new threshold.
    prices = make_prices(220, daily_return=0.0004)  # moderately rising
    state = rv5.V5StreakState()
    for i in range(201, len(prices) + 1):
        bucket, sig = rv5.step_v5(cfg, state, prices[:i])
    assert cfg["V5_UP_RATIO"] <= sig["ratio"] < cfg["V5_STRONG_UP_RATIO"]
    assert bucket == "UP"


def test_classifies_sideways_between_down_and_up_ratio():
    cfg = build_cfg_v5("DEV_EARLY", classifier_mode="THRESHOLD_EXT")
    prices = make_prices(220, daily_return=0.00005, noise=0.0)  # near-flat
    state = rv5.V5StreakState()
    for i in range(201, len(prices) + 1):
        bucket, sig = rv5.step_v5(cfg, state, prices[:i])
    assert cfg["V5_DOWN_RATIO"] < sig["ratio"] < cfg["V5_UP_RATIO"]
    assert bucket == "SIDEWAYS"


def test_threshold_ext_classifies_down_and_strong_down_symmetric():
    cfg = build_cfg_v5("DEV_EARLY", classifier_mode="THRESHOLD_EXT")
    # Rally then decline, so MA200 sits well above the eventual trough,
    # giving a ratio comfortably below both DOWN and STRONG_DOWN thresholds.
    rally = make_prices(220, start=100.0, daily_return=0.003)
    decline = rally[-1] * np.exp(np.cumsum(np.full(40, -0.012)))
    series = np.r_[rally, decline]
    state = rv5.V5StreakState()
    buckets = []
    for i in range(201, len(series) + 1):
        bucket, sig = rv5.step_v5(cfg, state, series[:i])
        buckets.append((bucket, sig["ratio"]))
    final_bucket, final_ratio = buckets[-1]
    assert final_ratio <= cfg["V5_STRONG_DOWN_RATIO"]
    assert final_bucket == "STRONG_DOWN"
    # Somewhere along the decline it must have passed through plain DOWN
    # before reaching STRONG_DOWN (monotonic weakening, not an instant jump).
    assert any(b == "DOWN" for b, _ in buckets)


def test_momentum_vol_percentile_classifies_strong_up_at_high_percentile_roc():
    cfg = build_cfg_v5("DEV_EARLY", classifier_mode="MOMENTUM_VOL_PERCENTILE")
    # Accelerating rally: flat-ish history, then a sharp recent acceleration,
    # so today's ret63 sits at the top of its own trailing-252-day history.
    base = make_prices(cfg["V5_PERCENTILE_LOOKBACK"] + 201, daily_return=0.0003)
    accel = base[-1] * np.exp(np.cumsum(np.full(80, 0.006)))
    series = np.r_[base, accel]
    state = rv5.V5StreakState()
    for i in range(201, len(series) + 1):
        bucket, sig = rv5.step_v5(cfg, state, series[:i])
    assert bucket == "STRONG_UP"


def test_momentum_vol_percentile_insufficient_history_falls_back_to_plain_up():
    """With less than V5_PERCENTILE_LOOKBACK+201 points, the percentile
    classifier can't compute a percentile and must conservatively fall back
    to plain UP/DOWN (never STRONG) rather than erroring."""
    cfg = build_cfg_v5("DEV_EARLY", classifier_mode="MOMENTUM_VOL_PERCENTILE", strong_up_rule="THRESHOLD")
    prices = make_prices(220, daily_return=0.004)  # would be STRONG_UP under THRESHOLD_EXT
    assert len(prices) < cfg["V5_PERCENTILE_LOOKBACK"] + 201
    state = rv5.V5StreakState()
    for i in range(201, len(prices) + 1):
        bucket, sig = rv5.step_v5(cfg, state, prices[:i])
    assert bucket == "UP"


def test_step_v5_insufficient_qqq_history_returns_none():
    cfg = build_cfg_v5("DEV_EARLY")
    state = rv5.V5StreakState()
    bucket, sig = rv5.step_v5(cfg, state, np.ones(50))
    assert bucket is None and sig is None


# ---------------------------------------------------------------- SPMO slot signal (UP bucket only)

def test_spmo_slot_enters_on_confirmed_bull_and_exits_on_confirmed_exit():
    cfg = build_cfg_v5("DEV_EARLY")
    rally = make_prices(220, daily_return=0.004)
    # 80 days (not 60): V5_CONFIRM_DAYS=4 (raised from the sensitivity sweep,
    # see config_v5.py's comment) needs a longer decline tail for the exit
    # confirmation streak to accumulate 4 consecutive qualifying days.
    decline = rally[-1] * np.exp(np.cumsum(np.full(80, -0.004)))
    series = np.r_[rally, decline]
    state = rv5.V5StreakState()
    slots = []
    for i in range(201, len(series) + 1):
        slot, sig = rv5.spmo_slot_signal(cfg, state, series[:i])
        slots.append(slot)
    assert slots[-len(decline):][-1] == "QQQM"  # confirmed exit by the end
    assert any(s == "SPMO" for s in slots)  # entered at some point during the rally


def test_spmo_slot_falls_back_to_qqqm_when_spmo_history_insufficient():
    cfg = build_cfg_v5("DEV_EARLY")
    state = rv5.V5StreakState()
    slot, sig = rv5.spmo_slot_signal(cfg, state, np.ones(50))
    assert slot == "QQQM" and sig is None


# ---------------------------------------------------------------- continuous allocation

def test_up_bucket_tilt_clamped_and_monotonic():
    cfg = build_cfg_v5("DEV_EARLY")
    lo, hi = cfg["V5_UP_RATIO"], cfg["V5_STRONG_UP_RATIO"]
    below = {"ratio": lo - 0.05}
    at_lo = {"ratio": lo}
    mid = {"ratio": (lo + hi) / 2}
    at_hi = {"ratio": hi}
    above = {"ratio": hi + 0.05}

    share_below, t_below = rv5.up_bucket_tilt(cfg, below)
    share_lo, t_lo = rv5.up_bucket_tilt(cfg, at_lo)
    share_mid, t_mid = rv5.up_bucket_tilt(cfg, mid)
    share_hi, t_hi = rv5.up_bucket_tilt(cfg, at_hi)
    share_above, t_above = rv5.up_bucket_tilt(cfg, above)

    assert share_below == share_lo == cfg["V5_UP_GPIQ_MAX"]  # clamped at/below lo
    assert share_hi == share_above == cfg["V5_UP_GPIQ_MIN"]  # clamped at/above hi
    assert t_below == t_lo == 0.0
    assert t_hi == t_above == 1.0
    assert share_lo > share_mid > share_hi  # monotonically decreasing in ratio


def test_cash_ratio_vol_target_clamped_to_min_scale_and_one():
    cfg = build_cfg_v5("DEV_EARLY", cash_ratio_mode="VOL_TARGET")
    # Very high realized vol -> scale clamped to the floor.
    scale_high_vol = rv5.cash_ratio_scale(cfg, "VOL_TARGET", "UP", {"ratio": 1.05}, 0.5, realized_annual_vol=5.0)
    assert scale_high_vol == pytest.approx(cfg["V5_CASH_MIN_SCALE"])
    # Very low realized vol -> scale clamped to 1.0 (never above full invest).
    scale_low_vol = rv5.cash_ratio_scale(cfg, "VOL_TARGET", "UP", {"ratio": 1.05}, 0.5, realized_annual_vol=0.001)
    assert scale_low_vol == pytest.approx(1.0)
    # None/zero vol (insufficient history) -> inert fallback to fully invested.
    assert rv5.cash_ratio_scale(cfg, "VOL_TARGET", "UP", {"ratio": 1.05}, 0.5, realized_annual_vol=None) == 1.0


def test_cash_ratio_vol_target_no_ticker_defaults_to_1x_multiplier():
    """Explicit regression guard: risk_ticker=None (or omitted) must exactly
    reproduce the pre-ticker-aware-fix formula (multiplier 1.0) -- this is
    the backward-compatibility contract the trailing optional parameter was
    designed to preserve, made explicit rather than merely inferred from the
    test above (which never passes risk_ticker) still passing."""
    cfg = build_cfg_v5("DEV_EARLY", cash_ratio_mode="VOL_TARGET")
    vol = 0.30
    expected = cfg["V5_VOL_TARGET_ANNUAL"] / vol
    assert rv5.cash_ratio_scale(cfg, "VOL_TARGET", "UP", {"ratio": 1.05}, 0.5, vol) == pytest.approx(expected)
    assert rv5.cash_ratio_scale(cfg, "VOL_TARGET", "UP", {"ratio": 1.05}, 0.5, vol, risk_ticker=None) == pytest.approx(expected)


def test_cash_ratio_vol_target_applies_ticker_leverage_multiplier():
    """TQQQ's effective target is 3x the base V5_VOL_TARGET_ANNUAL, since its
    realized vol is structurally ~3x QQQ's by design (3x leveraged fund) --
    without this, TQQQ's invest_scale was permanently pinned near
    V5_CASH_MIN_SCALE regardless of actual trend strength (confirmed
    empirically: STRONG_UP days held TQQQ at a mean weight of only 0.291 vs.
    a theoretical max ~0.78 before this fix)."""
    cfg = build_cfg_v5("DEV_EARLY", cash_ratio_mode="VOL_TARGET")
    # Chosen so the OLD (ticker-blind) formula clamps near the floor
    # (0.15/0.40 ~= 0.375, just above V5_CASH_MIN_SCALE=0.3) while the NEW
    # (3x-multiplied) formula does not (0.15*3/0.40 = 1.125, clamped to 1.0).
    vol = 0.40
    old_scale = float(np.clip(cfg["V5_VOL_TARGET_ANNUAL"] / vol, cfg["V5_CASH_MIN_SCALE"], 1.0))
    new_scale = rv5.cash_ratio_scale(cfg, "VOL_TARGET", "STRONG_UP", {"ratio": 1.1}, None, vol, risk_ticker="TQQQ")
    expected = float(np.clip(cfg["V5_VOL_TARGET_ANNUAL"] * 3.0 / vol, cfg["V5_CASH_MIN_SCALE"], 1.0))
    assert new_scale == pytest.approx(expected)
    assert new_scale > old_scale


def test_cash_ratio_vol_target_qid_uses_2x_multiplier():
    cfg = build_cfg_v5("DEV_EARLY", cash_ratio_mode="VOL_TARGET")
    vol = 0.60  # chosen so QID's 2x and TQQQ's 3x multiplier clearly diverge, neither clamped
    scale = rv5.cash_ratio_scale(cfg, "VOL_TARGET", "STRONG_DOWN", {"ratio": 0.9}, None, vol, risk_ticker="QID")
    expected = float(np.clip(cfg["V5_VOL_TARGET_ANNUAL"] * 2.0 / vol, cfg["V5_CASH_MIN_SCALE"], 1.0))
    assert scale == pytest.approx(expected)
    # Distinguishes QID's 2x from TQQQ's 3x -- not the same multiplier.
    tqqq_scale = rv5.cash_ratio_scale(cfg, "VOL_TARGET", "STRONG_UP", {"ratio": 1.1}, None, vol, risk_ticker="TQQQ")
    assert tqqq_scale != pytest.approx(scale)


def test_cash_ratio_vol_target_psq_multiplier_is_1x():
    """PSQ (-1x) is not leveraged in magnitude -- realized vol via np.std is
    sign-agnostic, so an inverse (non-leveraged) ETF's vol is already
    comparable to QQQ's own; it should get no multiplier adjustment."""
    cfg = build_cfg_v5("DEV_EARLY", cash_ratio_mode="VOL_TARGET")
    vol = 0.20
    scale = rv5.cash_ratio_scale(cfg, "VOL_TARGET", "DOWN", {"ratio": 0.9}, None, vol, risk_ticker="PSQ")
    expected = float(np.clip(cfg["V5_VOL_TARGET_ANNUAL"] / vol, cfg["V5_CASH_MIN_SCALE"], 1.0))
    assert scale == pytest.approx(expected)


def test_cash_ratio_signal_strength_clamped_at_extremes_and_matches_up_bucket_t():
    cfg = build_cfg_v5("DEV_EARLY", cash_ratio_mode="SIGNAL_STRENGTH")
    min_scale = cfg["V5_CASH_MIN_SCALE"]
    # t=0 (just entered UP) -> invest_scale == min_scale.
    assert rv5.cash_ratio_scale(cfg, "SIGNAL_STRENGTH", "UP", None, 0.0, None) == pytest.approx(min_scale)
    # t=1 (at STRONG_UP edge) -> invest_scale == 1.0.
    assert rv5.cash_ratio_scale(cfg, "SIGNAL_STRENGTH", "UP", None, 1.0, None) == pytest.approx(1.0)
    # STRONG_UP exactly at the boundary -> overshoot 0 -> min_scale.
    qqq_sig_at_boundary = {"ratio": cfg["V5_STRONG_UP_RATIO"]}
    assert rv5.cash_ratio_scale(cfg, "SIGNAL_STRENGTH", "STRONG_UP", qqq_sig_at_boundary, None, None) == pytest.approx(min_scale)
    # STRONG_UP well past the saturation band -> clamped to 1.0.
    qqq_sig_far = {"ratio": cfg["V5_STRONG_UP_RATIO"] + 10 * cfg["V5_SIGNAL_SATURATION_BAND"]}
    assert rv5.cash_ratio_scale(cfg, "SIGNAL_STRENGTH", "STRONG_UP", qqq_sig_far, None, None) == pytest.approx(1.0)
    # SIDEWAYS is inert (always 1.0 regardless of inputs).
    assert rv5.cash_ratio_scale(cfg, "SIGNAL_STRENGTH", "SIDEWAYS", None, None, None) == 1.0


# ---------------------------------------------------------------- GPIQ floor invariant (highest priority)

def _all_bucket_scenarios(cfg):
    """(bucket, qqq_sig, spmo_slot) combinations spanning every bucket at
    both ends of its own ratio range, used to stress effective_weights_v5()
    independent of invest_scale/up_t (those are swept separately below)."""
    scenarios = []
    for bucket, ratio in [
        ("SIDEWAYS", (cfg["V5_UP_RATIO"] + cfg["V5_DOWN_RATIO"]) / 2),
        ("UP", cfg["V5_UP_RATIO"]),
        ("UP", cfg["V5_STRONG_UP_RATIO"]),
        ("STRONG_UP", cfg["V5_STRONG_UP_RATIO"] + 0.05),
        ("DOWN", cfg["V5_DOWN_RATIO"]),
        ("DOWN", cfg["V5_STRONG_DOWN_RATIO"]),
        ("STRONG_DOWN", cfg["V5_STRONG_DOWN_RATIO"] - 0.05),
    ]:
        for spmo_slot in ("SPMO", "QQQ"):
            scenarios.append((bucket, {"ratio": ratio}, spmo_slot))
    return scenarios


def test_gpiq_floor_never_violated_across_all_five_buckets():
    # Trimmed slice left as idle cash, so the total below is exact (the
    # default parks it in SGOV/QQQM -- see the derisk-asset tests).
    cfg = build_cfg_v5("DEV_EARLY", up_derisk_asset="CASH", strong_derisk_asset="CASH")
    discretionary_max = 1.0 - cfg["V5_GPIQ_FLOOR"] - cfg["V5_CASH_BUFFER"]
    for bucket, qqq_sig, spmo_slot in _all_bucket_scenarios(cfg):
        for invest_scale in (cfg["V5_CASH_MIN_SCALE"], 1.0):
            weights = rv5.effective_weights_v5(cfg, bucket, qqq_sig, spmo_slot, invest_scale)
            assert weights["GPIQ"] >= cfg["V5_GPIQ_FLOOR"] - 1e-9, (bucket, qqq_sig, spmo_slot, invest_scale)
            # Total allocated weight is always exactly GPIQ_FLOOR + the
            # invested slice of the discretionary budget -- never more
            # (idle cash absorbs the rest), confirming no weight is
            # fabricated or lost by the GPIQ-floor/tilt/cash composition.
            expected_total = cfg["V5_GPIQ_FLOOR"] + discretionary_max * invest_scale
            assert sum(weights.values()) == pytest.approx(expected_total)


def test_effective_weights_v5_sideways_holds_only_gpiq():
    cfg = build_cfg_v5("DEV_EARLY")
    weights = rv5.effective_weights_v5(cfg, "SIDEWAYS", {"ratio": 1.0}, "QQQ", invest_scale=1.0)
    assert set(weights) == {"GPIQ"}
    assert weights["GPIQ"] == pytest.approx(1.0 - cfg["V5_CASH_BUFFER"])


def test_effective_weights_v5_strong_buckets_use_leveraged_ticker():
    cfg = build_cfg_v5("DEV_EARLY", strong_up_asset="TQQQ", down_asset="PSQ", strong_down_asset="QID")
    w_up = rv5.effective_weights_v5(cfg, "STRONG_UP", {"ratio": 1.2}, "QQQ", invest_scale=1.0)
    assert "TQQQ" in w_up and "QQQ" not in w_up and "SPMO" not in w_up
    w_down = rv5.effective_weights_v5(cfg, "DOWN", {"ratio": 0.95}, "QQQ", invest_scale=1.0)
    assert "PSQ" in w_down
    w_strong_down = rv5.effective_weights_v5(cfg, "STRONG_DOWN", {"ratio": 0.85}, "QQQ", invest_scale=1.0)
    assert "QID" in w_strong_down


def test_bucket_assets_are_configurable_and_route_to_the_chosen_ticker():
    cfg = build_cfg_v5("DEV_EARLY", strong_up_asset="QLD", down_asset="BIL", strong_down_asset="PSQ")
    assert "QLD" in rv5.effective_weights_v5(cfg, "STRONG_UP", {"ratio": 1.2}, "QQQ", 1.0)
    assert "BIL" in rv5.effective_weights_v5(cfg, "DOWN", {"ratio": 0.95}, "QQQ", 1.0)
    assert "PSQ" in rv5.effective_weights_v5(cfg, "STRONG_DOWN", {"ratio": 0.85}, "QQQ", 1.0)


def test_spmo_slot_strong_up_asset_follows_the_up_bucket_pick():
    cfg = build_cfg_v5("DEV_EARLY", strong_up_asset="SPMO_SLOT")
    assert "SPMO" in rv5.effective_weights_v5(cfg, "STRONG_UP", {"ratio": 1.2}, "SPMO", 1.0)
    assert "QQQ" in rv5.effective_weights_v5(cfg, "STRONG_UP", {"ratio": 1.2}, "QQQ", 1.0)


def test_cash_bucket_asset_leaves_the_risk_slice_uninvested_but_keeps_gpiq_floor():
    cfg = build_cfg_v5("DEV_EARLY", down_asset="CASH", strong_down_asset="CASH")
    for bucket, ratio in (("DOWN", 0.95), ("STRONG_DOWN", 0.85)):
        weights = rv5.effective_weights_v5(cfg, bucket, {"ratio": ratio}, "QQQ", 1.0)
        assert weights == {"GPIQ": cfg["V5_GPIQ_FLOOR"]}
        assert rv5.bucket_risk_ticker(cfg, bucket, "QQQ") is None


def test_gpiq_bucket_asset_merges_into_the_floor_position():
    cfg = build_cfg_v5("DEV_EARLY", down_asset="GPIQ")
    weights = rv5.effective_weights_v5(cfg, "DOWN", {"ratio": 0.95}, "QQQ", 1.0)
    assert set(weights) == {"GPIQ"}
    assert weights["GPIQ"] == pytest.approx(1.0 - cfg["V5_CASH_BUFFER"])


def test_build_cfg_v5_rejects_invalid_bucket_assets():
    with pytest.raises(ValueError, match="V5_STRONG_UP_ASSET"):
        build_cfg_v5("DEV_EARLY", strong_up_asset="PSQ")
    with pytest.raises(ValueError, match="V5_DOWN_ASSET"):
        build_cfg_v5("DEV_EARLY", down_asset="TQQQ")
    with pytest.raises(ValueError, match="V5_STRONG_DOWN_ASSET"):
        build_cfg_v5("DEV_EARLY", strong_down_asset="BOGUS")


def test_bucket_asset_defaults_lever_only_strong_up_and_never_short():
    """Config F (see config_v5.py): STRONG_UP holds TQQQ, both down buckets
    hold SGOV -- leverage only in STRONG_UP, never a short position."""
    cfg = build_cfg_v5("DEV_EARLY")
    assert (cfg["V5_STRONG_UP_ASSET"], cfg["V5_DOWN_ASSET"], cfg["V5_STRONG_DOWN_ASSET"]) == ("TQQQ", "SGOV", "SGOV")
    for bucket, ratio in (("UP", 1.05), ("SIDEWAYS", 1.0), ("DOWN", 0.95), ("STRONG_DOWN", 0.85)):
        weights = rv5.effective_weights_v5(cfg, bucket, {"ratio": ratio}, "QQQ", 1.0)
        assert not {"TQQQ", "QLD", "PSQ", "QID"} & set(weights), bucket
    assert "TQQQ" in rv5.effective_weights_v5(cfg, "STRONG_UP", {"ratio": 1.2}, "QQQ", 1.0)


def test_effective_weights_v5_up_bucket_holds_spmo_slot_ticker():
    cfg = build_cfg_v5("DEV_EARLY", up_slot_mode="SPMO_GATED")
    w_spmo = rv5.effective_weights_v5(cfg, "UP", {"ratio": cfg["V5_UP_RATIO"]}, "SPMO", invest_scale=1.0)
    assert "SPMO" in w_spmo and "QQQ" not in w_spmo
    w_qqq = rv5.effective_weights_v5(cfg, "UP", {"ratio": cfg["V5_UP_RATIO"]}, "QQQ", invest_scale=1.0)
    assert "QQQ" in w_qqq and "SPMO" not in w_qqq


# ---------------------------------------------------------------- GPIQ pre-listing proxy

def test_effective_targets_v5_routes_gpiq_through_proxy_before_listing():
    cfg = build_cfg_v5("DEV_EARLY")
    before_listing = date(2020, 1, 1)
    assert before_listing < date(*GPIQ_AVAILABLE_FROM)
    weights = {"GPIQ": 0.3, "SPMO": 0.5}
    targets, proxy_weight = rv5.effective_targets_v5(cfg, weights, before_listing)
    assert "GPIQ" not in targets
    assert targets["QQQ"] == pytest.approx(0.3)
    assert targets["SPMO"] == pytest.approx(0.5)
    assert proxy_weight == pytest.approx(0.3)


def test_effective_targets_v5_holds_gpiq_directly_after_listing():
    cfg = build_cfg_v5("DEV_RECENT")
    after_listing = date(2024, 1, 1)
    assert after_listing > date(*GPIQ_AVAILABLE_FROM)
    weights = {"GPIQ": 0.3, "SPMO": 0.5}
    targets, proxy_weight = rv5.effective_targets_v5(cfg, weights, after_listing)
    assert targets == {"GPIQ": 0.3, "SPMO": 0.5}
    assert proxy_weight == 0.0


# ---------------------------------------------------------------- new-ticker inception sanity

def test_tqqq_psq_qid_inception_dates_precede_dev_early_lookback():
    """Pure-arithmetic guard against a future PERIODS change silently
    breaking the plan's "no proxy-substitution needed for TQQQ/PSQ/QID"
    assumption (unlike GPIQ, which genuinely needs one -- see the proxy
    tests above). Inception dates per the plan's research phase: TQQQ
    2010-02-09, PSQ 2006-06-19, QID 2006-07-11."""
    cfg = build_cfg_v5("DEV_EARLY")
    dev_early_start = date(*cfg["PERIODS"]["DEV_EARLY"][0])
    lookback_floor = dev_early_start.toordinal() - 290  # ~200 trading days
    for inception in [date(2010, 2, 9), date(2006, 6, 19), date(2006, 7, 11)]:
        assert inception.toordinal() < lookback_floor


# ---------------------------------------------------------------- whipsaw-suppression regression (sensitivity sweep's fix)

def test_two_day_strong_up_spike_does_not_round_trip_under_new_defaults():
    """Direct regression guard for a real, confirmed bad episode: DEV_EARLY,
    2016-06-08/09, a 2-day SIDEWAYS->STRONG_UP->SIDEWAYS flicker forced a
    round-trip liquidation into TQQQ and back right before that month's
    worst drawdown. This constructs a synthetic series of the same shape
    (near-flat SIDEWAYS history, a brief 2-day spike pushing the ratio well
    over the STRONG_UP threshold, then reverting) and asserts the bucket
    sequence under the NEW defaults (V5_CONFIRM_DAYS=4, V5_STRONG_UP_RATIO=
    1.06, chosen via the --v5-sensitivity-matrix sweep) never visits
    STRONG_UP -- confirmed empirically to fix the real episode under both
    classifier_modes (THRESHOLD_EXT and MOMENTUM_VOL_PERCENTILE)."""
    for classifier_mode in ("THRESHOLD_EXT", "MOMENTUM_VOL_PERCENTILE"):
        cfg = build_cfg_v5("DEV_EARLY", classifier_mode=classifier_mode)
        history = make_prices(230, daily_return=0.00005)  # near-flat -> SIDEWAYS
        spike = history[-1] * np.exp(np.cumsum([0.03, 0.03]))  # sharp 2-day jump
        revert = spike[-1] * np.exp(np.cumsum([-0.03, -0.03, -0.001, -0.001, -0.001]))
        series = np.r_[history, spike, revert]
        state = rv5.V5StreakState()
        buckets = []
        for i in range(201, len(series) + 1):
            bucket, _ = rv5.step_v5(cfg, state, series[:i])
            buckets.append(bucket)
        assert "STRONG_UP" not in buckets, classifier_mode


def test_down_confirm_days_is_independent_of_up_confirm_days():
    """V5_DOWN_CONFIRM_DAYS (default 2) and V5_CONFIRM_DAYS (default 4,
    UP-family only) must be fully decoupled streaks -- this is the direct
    fix for a real bug: raising the shared confirm_days from 2 to 4 (to stop
    a UP<->STRONG_UP whipsaw) had the unintended side effect of also
    delaying DOWN confirmation, which during the real 2018-10/11 QQQ
    correction pushed DOWN confirmation from 2018-10-31 to 2018-11-23 (23
    extra days nearly fully invested in GPIQ while QQQ fell -6.25%).

    This constructs a synthetic series oscillating around V5_DOWN_RATIO
    (touches below, bounces back, touches again) and asserts: (a) DOWN
    confirms after exactly V5_DOWN_CONFIRM_DAYS=2 consecutive qualifying
    days, matching the faster pre-whipsaw-fix speed, and (b) the `up`
    streak counter (driven by the separate, unaffected V5_CONFIRM_DAYS)
    never advances during this purely-downside oscillation, proving the two
    confirmation counters are genuinely independent."""
    cfg = build_cfg_v5("DEV_EARLY")
    assert cfg["V5_DOWN_CONFIRM_DAYS"] == 2
    assert cfg["V5_CONFIRM_DAYS"] == 4
    history = make_prices(230, daily_return=0.0001)  # near-flat -> SIDEWAYS
    # Touch below V5_DOWN_RATIO for 2 days, bounce back above for 1 day
    # (resets the streak), then touch below again for 3 more days --
    # confirmation should land exactly 2 days into this second run.
    moves = [-0.015, -0.015, 0.015, -0.015, -0.015, -0.015]
    series = history.copy()
    for m in moves:
        series = np.r_[series, series[-1] * np.exp(m)]
    state = rv5.V5StreakState()
    buckets, down_streaks, up_streaks = [], [], []
    for i in range(201, len(series) + 1):
        bucket, _ = rv5.step_v5(cfg, state, series[:i])
        buckets.append(bucket)
        down_streaks.append(state.signal_streak["down"])
        up_streaks.append(state.signal_streak["up"])
    assert buckets[-1] == "DOWN"
    assert buckets[-2] == "SIDEWAYS"  # confirms exactly on the 2nd consecutive qualifying day
    assert down_streaks[-1] == cfg["V5_DOWN_CONFIRM_DAYS"]
    assert all(s == 0 for s in up_streaks)  # purely downside move never advances the UP streak


def test_strong_up_boundary_width_does_not_delay_genuine_sustained_rally():
    """Companion to the regression test above: confirms the new, narrower
    V5_STRONG_UP_RATIO=1.06 does NOT over-correct into "STRONG_UP never
    fires" for a genuinely sustained rally (guards against the whipsaw fix
    making the classifier too conservative in the opposite direction)."""
    cfg = build_cfg_v5("DEV_EARLY", classifier_mode="THRESHOLD_EXT")
    series = make_prices(220, daily_return=0.004)  # sustained strong rally
    state = rv5.V5StreakState()
    bucket = None
    for i in range(201, len(series) + 1):
        bucket, _ = rv5.step_v5(cfg, state, series[:i])
    assert bucket == "STRONG_UP"

    # MOMENTUM_VOL_PERCENTILE needs V5_PERCENTILE_LOOKBACK+201 points before
    # it can classify STRONG at all (shorter series fall back to plain UP,
    # the conservative-failure shape tested separately above) -- a flat-ish
    # history followed by a genuine acceleration exercises the percentile
    # classifier's own "real sustained strength" path.
    cfg_pct = build_cfg_v5("DEV_EARLY", classifier_mode="MOMENTUM_VOL_PERCENTILE")
    needed = cfg_pct["V5_PERCENTILE_LOOKBACK"] + 201
    base = make_prices(needed, daily_return=0.0005)
    accel = base[-1] * np.exp(np.cumsum(np.full(60, 0.004)))
    series_pct = np.r_[base, accel]
    state_pct = rv5.V5StreakState()
    bucket_pct = None
    for i in range(201, len(series_pct) + 1):
        bucket_pct, _ = rv5.step_v5(cfg_pct, state_pct, series_pct[:i])
    assert bucket_pct == "STRONG_UP"


# ---------------------------------------------------------------- seven-level buckets (CHOPPY_UP / CHOPPY_DOWN)

def _run_buckets(cfg, series):
    state = rv5.V5StreakState()
    out = []
    for i in range(201, len(series) + 1):
        bucket, sig = rv5.step_v5(cfg, state, series[:i])
        out.append((bucket, sig["ratio"]))
    return out


def test_five_mode_never_emits_choppy_buckets():
    cfg = build_cfg_v5("DEV_EARLY")
    assert cfg["V5_BUCKET_MODE"] == "FIVE"
    for daily in (0.0001, 0.00003, -0.00003):
        buckets = {b for b, _ in _run_buckets(cfg, make_prices(230, daily_return=daily))}
        assert not {"CHOPPY_UP", "CHOPPY_DOWN"} & buckets


def test_ratio_band_splits_sideways_by_ratio():
    cfg = build_cfg_v5("DEV_EARLY", bucket_mode="RATIO_BAND", overrides={"V5_UP_RATIO": 1.03})
    # ratio ~1.02 (gentle uptrend) sits in [1.01, 1.03) -> CHOPPY_UP
    up_series = make_prices(230, daily_return=0.0002)
    bucket, ratio = _run_buckets(cfg, up_series)[-1]
    assert cfg["V5_CHOPPY_UP_RATIO"] <= ratio < cfg["V5_UP_RATIO"]
    assert bucket == "CHOPPY_UP"
    # same ratio under FIVE stays SIDEWAYS -- the split only refines SIDEWAYS
    assert _run_buckets(build_cfg_v5("DEV_EARLY", overrides={"V5_UP_RATIO": 1.03}), up_series)[-1][0] == "SIDEWAYS"
    # mirror: gentle downtrend, ratio in (0.97, 0.99] -> CHOPPY_DOWN
    down_series = make_prices(230, daily_return=-0.0002)
    bucket, ratio = _run_buckets(cfg, down_series)[-1]
    assert cfg["V5_DOWN_RATIO"] < ratio <= cfg["V5_CHOPPY_DOWN_RATIO"]
    assert bucket == "CHOPPY_DOWN"


def test_ratio_band_only_ever_relabels_days_five_mode_called_sideways():
    five, band = build_cfg_v5("DEV_EARLY"), build_cfg_v5("DEV_EARLY", bucket_mode="RATIO_BAND")
    for daily in (0.004, 0.0002, -0.0002, -0.003):
        series = make_prices(240, daily_return=daily)
        for (b5, _), (b7, _) in zip(_run_buckets(five, series), _run_buckets(band, series)):
            if b5 != "SIDEWAYS":
                assert b7 == b5
            else:
                assert b7 in ("CHOPPY_UP", "SIDEWAYS", "CHOPPY_DOWN")


def test_efficiency_ratio_extremes():
    straight = np.linspace(100, 120, 30)
    assert rv5.efficiency_ratio(straight, 20) == pytest.approx(1.0)
    zigzag = 100 + np.tile([0.0, 1.0], 15)
    assert rv5.efficiency_ratio(zigzag, 20) < 0.1
    assert rv5.efficiency_ratio(straight[:5], 20) is None


def test_efficiency_mode_demotes_a_noisy_uptrend_but_not_a_clean_one():
    cfg = build_cfg_v5("DEV_EARLY", bucket_mode="EFFICIENCY", strong_up_rule="THRESHOLD")
    clean = make_prices(230, daily_return=0.0012)  # ratio ~1.06: UP, straight path
    assert _run_buckets(cfg, clean)[-1][0] == "UP"
    rng = np.random.default_rng(1)
    noisy = 100 * np.exp(np.cumsum(0.0012 + rng.normal(0, 0.03, 230)))
    noisy_bucket, noisy_ratio = _run_buckets(cfg, noisy)[-1]
    er = rv5.efficiency_ratio(noisy, cfg["V5_CHOPPY_ER_LOOKBACK"])
    if noisy_ratio >= cfg["V5_UP_RATIO"] and er < cfg["V5_CHOPPY_ER_MAX"]:
        assert noisy_bucket == "CHOPPY_UP"


def test_choppy_up_blend_is_continuous_with_sideways_and_up():
    """At the SIDEWAYS edge (ratio 1.0) CHOPPY_UP must hold exactly SIDEWAYS's
    weights; at the UP edge (ratio = V5_UP_RATIO) exactly UP's -- that is the
    dynamic blend between the two neighbours."""
    cfg = build_cfg_v5("DEV_EARLY", bucket_mode="RATIO_BAND")
    for slot in ("SPMO", "QQQ"):
        lo = rv5.effective_weights_v5(cfg, "CHOPPY_UP", {"ratio": 1.0}, slot, 1.0)
        assert lo == pytest.approx(rv5.effective_weights_v5(cfg, "SIDEWAYS", {"ratio": 1.0}, slot, 1.0))
        up_edge = {"ratio": cfg["V5_UP_RATIO"]}
        hi = rv5.effective_weights_v5(cfg, "CHOPPY_UP", up_edge, slot, 1.0)
        assert hi == pytest.approx(rv5.effective_weights_v5(cfg, "UP", up_edge, slot, 1.0))
        mid = rv5.effective_weights_v5(cfg, "CHOPPY_UP", {"ratio": 1.015}, slot, 1.0)
        assert lo["GPIQ"] > mid["GPIQ"] > hi["GPIQ"]  # leans further into the risk asset as ratio rises


def test_choppy_down_blend_is_continuous_with_sideways_and_down():
    cfg = build_cfg_v5("DEV_EARLY", bucket_mode="RATIO_BAND", down_asset="PSQ")
    lo = rv5.effective_weights_v5(cfg, "CHOPPY_DOWN", {"ratio": 1.0}, "QQQ", 1.0)
    assert lo == pytest.approx(rv5.effective_weights_v5(cfg, "SIDEWAYS", {"ratio": 1.0}, "QQQ", 1.0))
    edge = {"ratio": cfg["V5_DOWN_RATIO"]}
    hi = rv5.effective_weights_v5(cfg, "CHOPPY_DOWN", edge, "QQQ", 1.0)
    assert hi == pytest.approx(rv5.effective_weights_v5(cfg, "DOWN", edge, "QQQ", 1.0))
    mid = rv5.effective_weights_v5(cfg, "CHOPPY_DOWN", {"ratio": 0.985}, "QQQ", 1.0)
    assert 0 < mid["PSQ"] < hi["PSQ"]


def test_choppy_buckets_keep_gpiq_floor_and_total_weight():
    for mode in ("RATIO_BAND", "EFFICIENCY"):
        cfg = build_cfg_v5("DEV_EARLY", bucket_mode=mode, up_derisk_asset="CASH")
        expected_total = lambda s: cfg["V5_GPIQ_FLOOR"] + (1 - cfg["V5_GPIQ_FLOOR"] - cfg["V5_CASH_BUFFER"]) * s
        for bucket in ("CHOPPY_UP", "CHOPPY_DOWN"):
            for ratio in (0.97, 0.99, 1.0, 1.01, 1.03, 1.05):
                for scale in (cfg["V5_CASH_MIN_SCALE"], 1.0):
                    for er in (0.0, 0.1, 0.25):
                        w = rv5.effective_weights_v5(cfg, bucket, {"ratio": ratio}, "SPMO", scale, er)
                        assert w["GPIQ"] >= cfg["V5_GPIQ_FLOOR"] - 1e-9
                        assert sum(w.values()) == pytest.approx(expected_total(scale))


def test_sgov_routes_through_bil_before_listing():
    cfg = build_cfg_v5("DEV_EARLY", down_asset="SGOV")
    weights = {"GPIQ": 0.3, "SGOV": 0.5}
    # 2019: SGOV -> BIL, and GPIQ -> QQQM -> QQQ (QQQM not listed yet either)
    before, proxied = rv5.effective_targets_v5(cfg, weights, date(2019, 6, 1))
    assert before == pytest.approx({"QQQ": 0.3, "BIL": 0.5}) and proxied == pytest.approx(0.8)
    # 2021: SGOV and QQQM listed, GPIQ not -> GPIQ held as QQQM
    between, proxied = rv5.effective_targets_v5(cfg, weights, date(2021, 6, 1))
    assert between == pytest.approx({"QQQM": 0.3, "SGOV": 0.5}) and proxied == pytest.approx(0.3)
    after, proxied = rv5.effective_targets_v5(cfg, weights, date(2024, 6, 1))
    assert after == pytest.approx(weights) and proxied == 0.0


def test_qqqm_routes_through_qqq_before_its_listing():
    cfg = build_cfg_v5("DEV_EARLY")
    for day, expected in ((date(2020, 10, 12), "QQQ"), (date(2020, 10, 13), "QQQM")):
        targets, proxied = rv5.effective_targets_v5(cfg, {"QQQM": 0.6, "SPMO": 0.2}, day)
        assert targets == pytest.approx({expected: 0.6, "SPMO": 0.2})
        assert proxied == pytest.approx(0.6 if expected == "QQQ" else 0.0)


def test_effective_targets_v5_rejects_a_proxy_cycle():
    cfg = build_cfg_v5("DEV_EARLY")
    cfg["V5_PROXIES"] = {"AAA": ((2099, 1, 1), "BBB"), "BBB": ((2099, 1, 1), "AAA")}
    with pytest.raises(ValueError, match="cycle"):
        rv5.effective_targets_v5(cfg, {"AAA": 0.5}, date(2020, 1, 1))


def test_build_cfg_v5_rejects_unknown_bucket_mode():
    with pytest.raises(ValueError, match="V5_BUCKET_MODE"):
        build_cfg_v5("DEV_EARLY", bucket_mode="SEVEN")


def test_tradeable_ticker_follows_the_listing_chain():
    cfg = build_cfg_v5("DEV_EARLY")
    assert rv5.tradeable_ticker(cfg, "QQQM", date(2019, 1, 2)) == "QQQ"
    assert rv5.tradeable_ticker(cfg, "QQQM", date(2021, 1, 4)) == "QQQM"
    assert rv5.tradeable_ticker(cfg, "GPIQ", date(2019, 1, 2)) == "QQQ"    # GPIQ -> QQQM -> QQQ
    assert rv5.tradeable_ticker(cfg, "GPIQ", date(2022, 1, 3)) == "QQQM"   # QQQM listed, GPIQ not yet
    assert rv5.tradeable_ticker(cfg, "GPIQ", date(2024, 1, 2)) == "GPIQ"
    assert rv5.tradeable_ticker(cfg, "SGOV", date(2019, 1, 2)) == "BIL"
    assert rv5.tradeable_ticker(cfg, "SPMO", date(2016, 1, 4)) == "SPMO"   # never proxied


# ---------------------------------------------------------------- risk sleeves, strong-up rule, full-invest buckets

def test_sleeve_split_modes():
    cfg = build_cfg_v5("DEV_EARLY")
    ndx = cfg["V5_NDX_TICKER"]
    assert rv5.sleeve_split(cfg, "SPMO_GATED", "SPMO", None, None) == {"SPMO": 1.0}
    assert rv5.sleeve_split(cfg, "EQUAL", "SPMO", None, None) == {"SPMO": 0.5, ndx: 0.5}
    trend = rv5.sleeve_split(cfg, "TREND", "SPMO", {"ratio": 1.02}, {"ratio": 1.06})
    assert trend == pytest.approx({"SPMO": 0.75, ndx: 0.25})
    # neither above its MA200 -> 50/50; missing signal -> the gated pick
    assert rv5.sleeve_split(cfg, "TREND", "SPMO", {"ratio": 0.98}, {"ratio": 0.99}) == {"SPMO": 0.5, ndx: 0.5}
    assert rv5.sleeve_split(cfg, "TREND", ndx, None, {"ratio": 1.1}) == {ndx: 1.0}


def test_up_slot_equal_holds_both_spmo_and_qqqm_and_keeps_totals():
    cfg = build_cfg_v5("DEV_EARLY", up_slot_mode="EQUAL")
    base = build_cfg_v5("DEV_EARLY")
    for ratio in (1.03, 1.05, 1.06):
        w = rv5.effective_weights_v5(cfg, "UP", {"ratio": ratio}, "SPMO", 1.0)
        b = rv5.effective_weights_v5(base, "UP", {"ratio": ratio}, "SPMO", 1.0)
        assert w["SPMO"] == pytest.approx(w["QQQM"])
        assert w["GPIQ"] == pytest.approx(b["GPIQ"])            # GPIQ tilt unchanged
        assert sum(w.values()) == pytest.approx(sum(b.values()))


def test_strong_up_mix_asset_splits_the_risk_weight():
    cfg = build_cfg_v5("DEV_EARLY", strong_up_asset="SPMO_QQQM_EQUAL")
    w = rv5.effective_weights_v5(cfg, "STRONG_UP", {"ratio": 1.1}, "SPMO", 1.0)
    assert w["SPMO"] == pytest.approx(w["QQQM"]) and w["GPIQ"] == pytest.approx(cfg["V5_GPIQ_FLOOR"])


def test_efficiency_strong_up_rule_needs_a_straight_rising_path():
    cfg = build_cfg_v5("DEV_EARLY", strong_up_rule="EFFICIENCY")
    straight_up = make_prices(230, daily_return=0.0015)             # clean rally: STRONG_UP
    assert _run_buckets(cfg, straight_up)[-1][0] == "STRONG_UP"
    rng = np.random.default_rng(3)
    choppy_up = 100 * np.exp(np.cumsum(0.0015 + rng.normal(0, 0.03, 230)))
    bucket, ratio = _run_buckets(cfg, choppy_up)[-1]
    er = rv5.efficiency_ratio(choppy_up, cfg["V5_STRONG_ER_LOOKBACK"])
    if bucket in ("UP", "STRONG_UP"):
        assert (bucket == "STRONG_UP") == (er >= cfg["V5_STRONG_ER_MIN"] and choppy_up[-1] > choppy_up[-21])


def test_efficiency_rule_does_not_call_a_falling_path_strong_up():
    cfg = build_cfg_v5("DEV_EARLY", strong_up_rule="EFFICIENCY")
    rally = make_prices(230, daily_return=0.003)                     # ratio well above V5_UP_RATIO
    fall = rally[-1] * np.exp(np.cumsum(np.full(15, -0.004)))       # straight 15-day slide, still in uptrend
    series = np.r_[rally, fall]
    bucket, ratio = _run_buckets(cfg, series)[-1]
    assert ratio >= cfg["V5_UP_RATIO"]
    assert bucket == "UP"   # efficient, but downward -> not strong-up


def test_config_g_is_the_default():
    cfg = build_cfg_v5("DEV_EARLY")
    assert cfg["V5_STRONG_UP_RULE"] == "ALL_UP" and cfg["V5_STRONG_UP_ASSET"] == "TQQQ"
    assert cfg["V5_UP_SLOT_MODE"] == "EQUAL"
    assert (cfg["V5_UP_RATIO"], cfg["V5_UP_EXIT_RATIO"]) == (1.035, 1.01)   # config G2
    assert cfg["V5_REBALANCE_BAND"] == 0.05 and cfg["V5_CASH_BUFFER"] == 0.0
    assert (cfg["V5_UP_DERISK_ASSET"], cfg["V5_STRONG_DERISK_ASSET"]) == ("SGOV", "CASCADE")
    assert cfg["V5_CASH_RESERVE_USD"] == 13.0


def test_full_invest_buckets_skip_cash_scaling():
    cfg = build_cfg_v5("DEV_EARLY", full_invest_buckets=("UP", "STRONG_UP"))
    for bucket in ("UP", "STRONG_UP"):
        assert rv5.cash_ratio_scale(cfg, "VOL_TARGET", bucket, {"ratio": 1.05}, 0.5, 5.0, "SPMO") == 1.0
    # buckets not listed still scale down on high vol
    assert rv5.cash_ratio_scale(cfg, "VOL_TARGET", "DOWN", {"ratio": 0.95}, None, 5.0, "SGOV") == pytest.approx(
        cfg["V5_CASH_MIN_SCALE"])


def test_build_cfg_v5_validates_new_options():
    with pytest.raises(ValueError, match="V5_UP_SLOT_MODE"):
        build_cfg_v5("DEV_EARLY", up_slot_mode="BOTH")
    with pytest.raises(ValueError, match="V5_STRONG_UP_RULE"):
        build_cfg_v5("DEV_EARLY", strong_up_rule="FAST")
    with pytest.raises(ValueError, match="V5_FULL_INVEST_BUCKETS"):
        build_cfg_v5("DEV_EARLY", full_invest_buckets=("MOON",))
    with pytest.raises(ValueError, match="V5_REBALANCE_BAND"):
        build_cfg_v5("DEV_EARLY", rebalance_band=0.7)


def test_efficiency_rule_min_hold_and_hysteresis_stop_flicker():
    """A noisy uptrend with a short clean burst: without hold/hysteresis the
    raw efficiency signal flickers in and out of STRONG_UP; a minimum hold or
    a lower exit level keeps the episode continuous."""
    base = 100 * np.exp(np.cumsum(0.0012 + np.tile([0.012, -0.010], 115)))
    burst = base[-1] * np.exp(np.cumsum(np.full(8, 0.006)))
    after = burst[-1] * np.exp(np.cumsum(0.0012 + np.tile([0.012, -0.010], 10)))
    series = np.r_[base, burst, after]

    def flips(hold, exit_):
        cfg = build_cfg_v5("DEV_EARLY", strong_up_rule="EFFICIENCY",
                           overrides={"V5_STRONG_MIN_HOLD": hold, "V5_STRONG_ER_EXIT": exit_,
                                      "V5_STRONG_ER_MIN": 0.35})
        seq = [b for b, _ in _run_buckets(cfg, series)][-30:]
        return sum(1 for a, b in zip(seq, seq[1:]) if a != b), seq.count("STRONG_UP")

    raw_flips, raw_days = flips(0, 0.35)
    for hold, exit_ in ((5, 0.35), (0, 0.20)):
        f, d = flips(hold, exit_)
        assert f < raw_flips and d >= raw_days, (hold, exit_)


def test_min_hold_keeps_a_young_strong_up_episode():
    cfg = build_cfg_v5("DEV_EARLY", strong_up_rule="EFFICIENCY", overrides={"V5_STRONG_MIN_HOLD": 5})
    state = rv5.V5StreakState()
    rally = make_prices(240, daily_return=0.0015)
    for i in range(201, len(rally) + 1):
        rv5.step_v5(cfg, state, rally[:i])
    assert state.bucket == "STRONG_UP"
    state.strong_hold_days = 2                       # pretend the episode is only 2 days old
    chop = rally[-1] * np.exp(np.cumsum(np.tile([0.01, -0.01], 10)))   # er collapses to ~0
    bucket, _ = rv5.step_v5(cfg, state, np.r_[rally, chop])
    assert bucket == "STRONG_UP"                     # still inside the 5-day minimum hold


def test_strong_signal_source_reads_the_separate_series():
    """With strong_prices given, the EFFICIENCY strong-up decision reads that
    series, not the Nasdaq-100 one; the up-family gate stays on the Nasdaq-100."""
    cfg = build_cfg_v5("DEV_EARLY", strong_up_rule="EFFICIENCY", strong_signal_source="TQQQ")
    ndx = make_prices(240, daily_return=0.0015)                      # clean uptrend: would be STRONG_UP
    flat_strong = 100 + np.tile([0.0, 1.0], 120)                     # zig-zag: efficiency ~0
    state = rv5.V5StreakState()
    for i in range(201, len(ndx) + 1):
        bucket, _ = rv5.step_v5(cfg, state, ndx[:i], flat_strong[:i])
    assert bucket == "UP"                                             # uptrend, but the TQQQ series isn't strong
    state = rv5.V5StreakState()
    for i in range(201, len(ndx) + 1):
        bucket, _ = rv5.step_v5(cfg, state, ndx[:i], None)
    assert bucket == "STRONG_UP"                                      # no separate series -> Nasdaq-100 decides


def test_build_cfg_v5_rejects_unknown_strong_signal_source():
    with pytest.raises(ValueError, match="V5_STRONG_SIGNAL_SOURCE"):
        build_cfg_v5("DEV_EARLY", strong_signal_source="SPY")
    assert build_cfg_v5("DEV_EARLY")["V5_STRONG_SIGNAL_SOURCE"] == "NDX"


def test_volatility_spike_needs_high_vol_and_price_below_ma50():
    rng = np.random.default_rng(11)
    calm = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.005, 300)))
    assert not rv5.volatility_spike(calm, 1.5)
    shock_down = calm[-1] * np.exp(np.cumsum(rng.normal(-0.004, 0.03, 20)))   # vol x6, falling
    assert rv5.volatility_spike(np.r_[calm, shock_down], 1.5)
    shock_up = calm[-1] * np.exp(np.cumsum(np.abs(rng.normal(0.004, 0.03, 20))))  # vol up but rising
    assert not rv5.volatility_spike(np.r_[calm, shock_up], 1.5)              # above MA50 -> no signal
    assert not rv5.volatility_spike(calm[:100], 1.5)                          # not enough history


def test_early_down_volspike_turns_a_non_down_day_into_down():
    rng = np.random.default_rng(11)
    calm = 100 * np.exp(np.cumsum(rng.normal(0.0008, 0.005, 300)))
    series = np.r_[calm, calm[-1] * np.exp(np.cumsum(rng.normal(-0.004, 0.03, 20)))]
    off = build_cfg_v5("DEV_EARLY")
    on = build_cfg_v5("DEV_EARLY", early_down="VOLSPIKE")
    b_off = _run_buckets(off, series)[-1][0]
    b_on = _run_buckets(on, series)[-1][0]
    assert b_on in ("DOWN", "STRONG_DOWN")
    if b_off not in ("DOWN", "STRONG_DOWN"):
        assert b_on == "DOWN"


def test_early_down_defaults_off_and_validates():
    assert build_cfg_v5("DEV_EARLY")["V5_EARLY_DOWN"] == "NONE"
    with pytest.raises(ValueError, match="V5_EARLY_DOWN"):
        build_cfg_v5("DEV_EARLY", early_down="MA50")


# ---------------------------------------------------------------- where the trimmed slice goes

def test_derisk_asset_parks_the_trimmed_slice_instead_of_cash():
    """With invest_scale < 1, UP / STRONG_UP put the trimmed part of the
    discretionary slice into the derisk asset; the risk sleeve and GPIQ are
    unchanged, and the total becomes fully invested (1 - cash buffer)."""
    idle = build_cfg_v5("DEV_EARLY", up_derisk_asset="CASH", strong_derisk_asset="CASH")
    scale = 0.6
    trimmed = (1 - idle["V5_GPIQ_FLOOR"] - idle["V5_CASH_BUFFER"]) * (1 - scale)
    for bucket, asset, ticker in (("UP", "SGOV", "SGOV"), ("UP", "GPIQ", "GPIQ"),
                                  ("STRONG_UP", "NDX", "QQQM"), ("STRONG_UP", "SGOV", "SGOV")):
        key = "up_derisk_asset" if bucket == "UP" else "strong_derisk_asset"
        cfg = build_cfg_v5("DEV_EARLY", **{key: asset})
        base = rv5.effective_weights_v5(idle, bucket, {"ratio": 1.05}, "SPMO", scale)
        parked = rv5.effective_weights_v5(cfg, bucket, {"ratio": 1.05}, "SPMO", scale)
        assert parked[ticker] == pytest.approx(base.get(ticker, 0.0) + trimmed), (bucket, asset)
        for t in set(base) - {ticker}:
            assert parked[t] == pytest.approx(base[t]), (bucket, asset, t)
        assert sum(parked.values()) == pytest.approx(1 - cfg["V5_CASH_BUFFER"])


def test_derisk_asset_only_applies_to_up_buckets_and_only_when_trimmed():
    cfg = build_cfg_v5("DEV_EARLY", up_derisk_asset="SGOV", strong_derisk_asset="NDX",
                       down_asset="PSQ", strong_down_asset="QID")
    idle = build_cfg_v5("DEV_EARLY", up_derisk_asset="CASH", strong_derisk_asset="CASH",
                        down_asset="PSQ", strong_down_asset="QID")
    for bucket, ratio in (("SIDEWAYS", 1.0), ("DOWN", 0.95), ("STRONG_DOWN", 0.85)):
        assert (rv5.effective_weights_v5(cfg, bucket, {"ratio": ratio}, "SPMO", 0.5)
                == rv5.effective_weights_v5(idle, bucket, {"ratio": ratio}, "SPMO", 0.5)), bucket
    for bucket in ("UP", "STRONG_UP"):   # fully invested: nothing trimmed, nothing parked
        assert (rv5.effective_weights_v5(cfg, bucket, {"ratio": 1.05}, "SPMO", 1.0)
                == rv5.effective_weights_v5(idle, bucket, {"ratio": 1.05}, "SPMO", 1.0)), bucket


def test_derisk_asset_validation():
    with pytest.raises(ValueError, match="V5_UP_DERISK_ASSET"):
        build_cfg_v5("DEV_EARLY", up_derisk_asset="TQQQ")
    with pytest.raises(ValueError, match="V5_STRONG_DERISK_ASSET"):
        build_cfg_v5("DEV_EARLY", strong_derisk_asset="BOGUS")


def test_cascade_steps_strong_up_slice_down_through_up_sleeve_then_gpiq_then_sgov():
    cfg = build_cfg_v5("DEV_EARLY", strong_derisk_asset="CASCADE", up_slot_mode="EQUAL")
    scale = 0.5
    trimmed = (1 - cfg["V5_GPIQ_FLOOR"] - cfg["V5_CASH_BUFFER"]) * (1 - scale)
    w = rv5.effective_weights_v5(cfg, "STRONG_UP", {"ratio": 1.1}, "SPMO", scale, level_scales=[0.6, 0.5])
    to_up = trimmed * 0.6                     # UP's sleeve takes what its own vol target allows
    to_gpiq = (trimmed - to_up) * 0.5         # GPIQ takes its share of the rest
    to_sgov = trimmed - to_up - to_gpiq       # the remainder sits in T-bills
    assert w["SPMO"] == pytest.approx(to_up / 2) and w["QQQM"] == pytest.approx(to_up / 2)
    assert w["GPIQ"] == pytest.approx(cfg["V5_GPIQ_FLOOR"] + to_gpiq)
    assert w["SGOV"] == pytest.approx(to_sgov)
    assert sum(w.values()) == pytest.approx(1 - cfg["V5_CASH_BUFFER"])
    # every level fully accepting -> nothing reaches SGOV
    full = rv5.effective_weights_v5(cfg, "STRONG_UP", {"ratio": 1.1}, "SPMO", scale, level_scales=[1.0, 1.0])
    assert "SGOV" not in full and full["SPMO"] + full["QQQM"] == pytest.approx(trimmed)


def test_cascade_for_up_goes_to_gpiq_then_sgov():
    cfg = build_cfg_v5("DEV_EARLY", up_derisk_asset="CASCADE")
    idle = build_cfg_v5("DEV_EARLY", up_derisk_asset="CASH")
    trimmed = (1 - cfg["V5_GPIQ_FLOOR"] - cfg["V5_CASH_BUFFER"]) * 0.4
    base = rv5.effective_weights_v5(idle, "UP", {"ratio": 1.05}, "SPMO", 0.6)
    w = rv5.effective_weights_v5(cfg, "UP", {"ratio": 1.05}, "SPMO", 0.6, level_scales=[0.25])
    assert w["GPIQ"] == pytest.approx(base["GPIQ"] + trimmed * 0.25)
    assert w["SGOV"] == pytest.approx(trimmed * 0.75)
    assert rv5.derisk_levels(cfg, "SIDEWAYS", "SPMO") == [] and rv5.derisk_levels(cfg, "DOWN", "SPMO") == []


def test_vol_target_scale_matches_cash_ratio_scale():
    cfg = build_cfg_v5("DEV_EARLY")
    for vol, ticker in ((0.10, None), (0.30, "QQQM"), (0.90, "TQQQ"), (None, "SPMO")):
        assert rv5.vol_target_scale(cfg, vol, ticker) == rv5.cash_ratio_scale(cfg, "VOL_TARGET", "UP", {"ratio": 1.05},
                                                                              0.5, vol, ticker)


# ---------------------------------------------------------------- textbook strong-up rules

def test_all_up_makes_every_up_family_day_strong():
    cfg = build_cfg_v5("DEV_EARLY", strong_up_rule="ALL_UP")
    for daily in (0.0006, 0.002, 0.004):
        for bucket, ratio in _run_buckets(cfg, make_prices(240, daily_return=daily)):
            assert bucket != "UP"
            if bucket == "STRONG_UP":
                assert ratio >= cfg["V5_UP_RATIO"]


def test_textbook_rules_on_clean_rise_and_after_a_drop():
    rise = make_prices(300, daily_return=0.001)
    for rule in ("MA_STACK", "MOM_ALL", "NEAR_HIGH", "ALL_UP"):
        assert rv5.textbook_strong_up(rule, rise), rule
    dropped = np.r_[rise, rise[-1] * np.exp(np.cumsum(np.full(15, -0.01)))]   # -14% in 3 weeks
    for rule in ("MA_STACK", "MOM_ALL", "NEAR_HIGH"):
        assert not rv5.textbook_strong_up(rule, dropped), rule
    assert not rv5.textbook_strong_up("MOM_ALL", rise[:100])                 # not enough history


def test_calm_trend_needs_vol_below_its_own_median():
    rng = np.random.default_rng(3)
    calm_tail = 100 * np.exp(np.cumsum(np.r_[0.001 + rng.normal(0, 0.02, 280), 0.001 + rng.normal(0, 0.003, 20)]))
    wild_tail = 100 * np.exp(np.cumsum(np.r_[0.001 + rng.normal(0, 0.003, 280), 0.004 + rng.normal(0, 0.02, 20)]))
    assert rv5.textbook_strong_up("CALM_TREND", calm_tail)
    assert not rv5.textbook_strong_up("CALM_TREND", wild_tail)


def test_strength_votes_and_vote_rules():
    rise = make_prices(300, daily_return=0.001)
    assert rv5.strength_votes(rise) == sum(rv5.textbook_strong_up(r, rise) for r in rv5.VOTE_SIGNALS)
    for rule, need in (("VOTE_2", 2), ("VOTE_3", 3)):
        cfg = build_cfg_v5("DEV_EARLY", strong_up_rule=rule)
        bucket, _ = _run_buckets(cfg, rise)[-1]
        assert (bucket == "STRONG_UP") == (rv5.strength_votes(rise[:-1]) >= need)


def test_tqqq_graded_sleeve_scales_with_votes_and_leverage():
    cfg = build_cfg_v5("DEV_EARLY", strong_up_asset="TQQQ_GRADED")
    for votes in range(5):
        sleeve = rv5.bucket_sleeve(cfg, "STRONG_UP", "SPMO", {"ratio": 1.1, "strength_votes": votes})
        assert sleeve.get("TQQQ", 0.0) == pytest.approx(votes / 4)
        assert sum(sleeve.values()) == pytest.approx(1.0)
        assert rv5.sleeve_leverage(sleeve) == pytest.approx(1 + 2 * votes / 4)
    # the vol target scales with the sleeve's actual leverage
    half = rv5.bucket_sleeve(cfg, "STRONG_UP", "SPMO", {"ratio": 1.1, "strength_votes": 2})
    assert rv5.vol_target_scale(cfg, 0.60, leverage=rv5.sleeve_leverage(half)) == pytest.approx(0.15 * 2 / 0.60)
    assert rv5.sleeve_leverage({"TQQQ": 1.0}) == 3.0 and rv5.sleeve_leverage({}) == 1.0


def test_tqqq_qld_sleeve_is_half_each_at_2_5x():
    cfg = build_cfg_v5("DEV_EARLY", strong_up_asset="TQQQ_QLD")
    sleeve = rv5.bucket_sleeve(cfg, "STRONG_UP", "SPMO", {"ratio": 1.1})
    assert sleeve == {"TQQQ": 0.5, "QLD": 0.5}
    assert rv5.sleeve_leverage(sleeve) == pytest.approx(2.5)
    w = rv5.effective_weights_v5(cfg, "STRONG_UP", {"ratio": 1.1}, "SPMO", 1.0)
    assert w["TQQQ"] == pytest.approx(w["QLD"])


def test_up_exit_band_keeps_a_confirmed_uptrend_through_a_small_dip():
    """Entry needs ratio >= V5_UP_RATIO; once in, the up-family holds while
    the ratio stays >= V5_UP_EXIT_RATIO. Without the band the same day drops
    out. Driven from a confirmed uptrend, then a day whose ratio sits inside
    the band (between exit 1.01 and entry 1.035)."""
    rise = make_prices(240, daily_return=0.0012)
    hist = rise[:-1]
    ma = float(np.mean(hist[-200:]))
    dip = np.r_[hist, ma * 1.02]                                    # tomorrow's history: ratio ~1.02
    for cfg, expect_up in ((build_cfg_v5("DEV_EARLY"), True),
                           (build_cfg_v5("DEV_EARLY", overrides={"V5_UP_EXIT_RATIO": None}), False)):
        st = rv5.V5StreakState()
        for i in range(201, len(hist) + 1):
            rv5.step_v5(cfg, st, hist[:i])
        assert st.bucket in ("UP", "STRONG_UP")
        bucket, sig = rv5.step_v5(cfg, st, dip)
        assert cfg["V5_UP_EXIT_RATIO"] is None or cfg["V5_UP_EXIT_RATIO"] <= sig["ratio"] < cfg["V5_UP_RATIO"]
        assert (bucket in ("UP", "STRONG_UP")) == expect_up, cfg["V5_UP_EXIT_RATIO"]


def test_up_exit_band_does_not_lower_the_entry():
    flat = make_prices(240, daily_return=0.00012)                    # ratio ~1.015: inside the band, never entered
    bucket, ratio = _run_buckets(build_cfg_v5("DEV_EARLY"), flat)[-1]
    assert 1.01 <= ratio < 1.035 and bucket == "SIDEWAYS"

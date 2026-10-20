"""Single source of truth for SGS parameters.

Ported verbatim from reference/SGS_V3_2_BASE_main.py CFG (the four QuantConnect
main.py files are identical except for this block's EXPERIMENT value and the
module docstring). EXPERIMENT and RUN_MODE are passed as arguments instead of
being baked into four duplicated files.

Do not add hidden constants in engine.py/state_machine.py: every tunable value
must live here, exactly as in the original QC CFG, so a diff against
reference/SGS_V3_2_BASE_main.py stays meaningful.
"""
from __future__ import annotations

import copy

EXPERIMENTS = ("BASE", "EARLY_ONLY", "WEAK_ONLY", "BOTH")
RUN_MODES = ("DEV_EARLY", "DEV_RECENT", "FINAL_HOLDOUT")
TREND_SOURCES = ("QQQ", "SPMO")
SPMO_MODES = (None, "GATE", "TILT", "DUAL")
BEAR_LOGICS = ("ORIGINAL", "RECOVERY_ONLY_IMPROVED")
ATTACK_ASSETS = ("SPMO", "QQQ", "STRONGER")
ATTACK_ASSET_COMPARISONS = ("RATIO", "RET20")
ASSET_SWITCH_MODES = ("DAILY", "CONFIRM_N", "MARGIN_BAND", "LOCK_PER_EPISODE")
VOL_TARGET_MODES = ("NONE", "VOL_TARGET")
DIVIDEND_REINVEST_MODES = ("DEFERRED", "IMMEDIATE")

REQUIRED_REGIMES = {"ATTACK", "NEUTRAL", "WEAK", "BEAR", "RECOVERY", "EARLY_RECOVERY"}

# Whitelist for build_cfg(overrides=...): plain scalar thresholds only, never
# structural fields (ROLE_WEIGHTS/ACTUAL_AVAILABLE_FROM/PROXY_TICKER/etc) --
# this is a parameter-sensitivity probe, not a way to redefine the state
# machine's shape.
OVERRIDABLE_PARAMS = {
    "BULL_MA200_RATIO", "BULL_RETURN_63", "BEAR_MA200_RATIO", "BEAR_DRAWDOWN_126",
    "BEAR_CONFIRM_DAYS", "RECOVERY_MA200_RATIO", "RECOVERY_RETURN_63",
    "EXIT_MA50_RATIO", "EXIT_RETURN_63", "EXIT_DRAWDOWN_126",
    "WEAK_RETURN_63", "WEAK_DRAWDOWN_126", "CONFIRM_DAYS", "RECOVERY_WAIT_DAYS",
}

# Named position-allocation schemes (ROLE_WEIGHTS tables), a dimension
# orthogonal to EXPERIMENT (state-machine rule toggles) and TREND_SOURCE/
# SPMO_MODE (signal source). ORIGINAL is byte-for-byte the table ported from
# reference/SGS_V3_2_BASE_main.py CFG; it must never be edited in place.
ROLE_WEIGHT_SCHEMES = {
    # WEIGHTS SUM TO 0.98, leaving 2% cash target in each regime.
    "ORIGINAL": {
        "ATTACK":   {"GPIQ": 0.40, "SPMO": 0.58},
        "NEUTRAL":  {"GPIQ": 0.98},
        "WEAK":     {"GPIQ": 0.40, "JEPI": 0.58},
        "BEAR":     {"SGOV": 0.78, "JEPI": 0.20},
        "RECOVERY": {"GPIQ": 0.98},
        "EARLY_RECOVERY": {"SGOV": 0.40, "GPIQ": 0.40, "JEPI": 0.18},
    },
    # User-proposed scheme: ATTACK/NEUTRAL/RECOVERY share an IDENTICAL target
    # weight table, so switching between them never liquidates/rebuilds the
    # SPMO position (only a tiny rounding-level share adjustment) -- the
    # mechanism meant to stop regime churn from crossing SPMO's ex-dividend
    # dates. GPIQ is removed entirely; JEPI is kept only in BEAR. WEAK/
    # EARLY_RECOVERY are a 60:40 SGOV/SPMO split (mirrored), scaled to the
    # same 0.98 cash-buffer convention as every other scheme (60:40 -> 58.8%/
    # 39.2%).
    "CONSOLIDATED": {
        "ATTACK":   {"SPMO": 0.98},
        "NEUTRAL":  {"SPMO": 0.98},
        "RECOVERY": {"SPMO": 0.98},
        "WEAK":     {"SGOV": 0.588, "SPMO": 0.392},
        "EARLY_RECOVERY": {"SPMO": 0.588, "SGOV": 0.392},
        "BEAR":     {"SGOV": 0.78, "JEPI": 0.20},
    },
    # Variants of the same core idea (no GPIQ, ATTACK/NEUTRAL/RECOVERY share
    # one table to cut churn) exploring how defensive WEAK/EARLY_RECOVERY
    # should be, and what the BEAR sleeve's composition should be.

    # Maximal churn reduction: only TWO distinct tables exist at all --
    # everything except BEAR is pure risk-on SPMO (WEAK and EARLY_RECOVERY
    # join ATTACK/NEUTRAL/RECOVERY instead of holding a defensive sleeve).
    "BINARY": {
        "ATTACK":   {"SPMO": 0.98},
        "NEUTRAL":  {"SPMO": 0.98},
        "RECOVERY": {"SPMO": 0.98},
        "WEAK":     {"SPMO": 0.98},
        "EARLY_RECOVERY": {"SPMO": 0.98},
        "BEAR":     {"SGOV": 0.78, "JEPI": 0.20},
    },
    # More defensive than CONSOLIDATED: WEAK mirrors BEAR's own 78:20 ratio
    # (mostly SGOV) instead of a 60:40 split; EARLY_RECOVERY is close to
    # even (50:48) rather than already leaning offensive.
    "DEFENSIVE_WEAK": {
        "ATTACK":   {"SPMO": 0.98},
        "NEUTRAL":  {"SPMO": 0.98},
        "RECOVERY": {"SPMO": 0.98},
        "WEAK":     {"SGOV": 0.78, "SPMO": 0.20},
        "EARLY_RECOVERY": {"SPMO": 0.50, "SGOV": 0.48},
        "BEAR":     {"SGOV": 0.78, "JEPI": 0.20},
    },
    # Same WEAK/EARLY_RECOVERY split as CONSOLIDATED, but BEAR drops JEPI
    # entirely for a pure cash-equivalent defense (98% SGOV) -- tests
    # whether JEPI's dividend/lower-vol tilt is worth keeping even in the
    # worst regime, or whether it's better to just de-risk completely.
    "BEAR_CASH_ONLY": {
        "ATTACK":   {"SPMO": 0.98},
        "NEUTRAL":  {"SPMO": 0.98},
        "RECOVERY": {"SPMO": 0.98},
        "WEAK":     {"SGOV": 0.588, "SPMO": 0.392},
        "EARLY_RECOVERY": {"SPMO": 0.588, "SGOV": 0.392},
        "BEAR":     {"SGOV": 0.98},
    },
    # Swaps SGOV for JEPI as the defensive buffer in WEAK/EARLY_RECOVERY
    # (JEPI itself pays a dividend and has lower volatility than SPMO,
    # unlike SGOV which is a pure cash-equivalent) -- BEAR unchanged.
    "JEPI_BUFFER": {
        "ATTACK":   {"SPMO": 0.98},
        "NEUTRAL":  {"SPMO": 0.98},
        "RECOVERY": {"SPMO": 0.98},
        "WEAK":     {"JEPI": 0.588, "SPMO": 0.392},
        "EARLY_RECOVERY": {"SPMO": 0.588, "JEPI": 0.392},
        "BEAR":     {"SGOV": 0.78, "JEPI": 0.20},
    },
    # EARLY_RECOVERY joins the risk-on group outright (same table as
    # ATTACK/NEUTRAL/RECOVERY, since by construction it only triggers after
    # a confirmed bounce) -- only WEAK keeps a defensive sleeve.
    "FAST_RECOVERY": {
        "ATTACK":   {"SPMO": 0.98},
        "NEUTRAL":  {"SPMO": 0.98},
        "RECOVERY": {"SPMO": 0.98},
        "EARLY_RECOVERY": {"SPMO": 0.98},
        "WEAK":     {"SGOV": 0.588, "SPMO": 0.392},
        "BEAR":     {"SGOV": 0.78, "JEPI": 0.20},
    },
}
WEIGHT_SCHEMES = tuple(ROLE_WEIGHT_SCHEMES)

BASE_CFG = {
    "PERIODS": {
        "DEV_EARLY": ((2016, 1, 4), (2019, 12, 31)),
        "DEV_RECENT": ((2023, 10, 30), (2026, 10, 16)),
        "FINAL_HOLDOUT": ((2020, 1, 2), (2022, 12, 30)),
    },

    "STARTING_CASH_USD": 10000,
    "MONTHLY_DEPOSIT_USD": 200,
    "CASH_BUFFER": 0.02,
    "SLIPPAGE_PER_SIDE": 0.0005,
    "ANNUAL_RF_ASSUMPTION": 0.04,  # for displayed Sharpe only
    "RECOVERY_WAIT_DAYS": 20,

    # QQQ-derived market regime parameters
    "BULL_MA200_RATIO": 1.03,
    "BULL_RETURN_63": 0.05,
    "BEAR_MA200_RATIO": 0.97,
    "BEAR_DRAWDOWN_126": -0.18,
    "BEAR_CONFIRM_DAYS": 2,
    "RECOVERY_MA200_RATIO": 1.01,
    "RECOVERY_RETURN_63": 0.00,
    "EXIT_MA50_RATIO": 0.99,
    "EXIT_RETURN_63": 0.00,
    "EXIT_DRAWDOWN_126": -0.10,
    "WEAK_RETURN_63": -0.03,
    "WEAK_DRAWDOWN_126": -0.08,
    "CONFIRM_DAYS": 2,

    # V4-A: Bear -> EARLY_RECOVERY -> full GPIQ recovery.
    "EARLY_MA20_RATIO": 1.00,
    "EARLY_RETURN_5": 0.00,
    "EARLY_LOW20_BOUNCE": 0.05,
    "EARLY_CONFIRM_DAYS": 2,
    "EARLY_TO_FULL_MA50_RATIO": 1.00,
    "EARLY_TO_FULL_RETURN_20": 0.00,
    "EARLY_TO_FULL_CONFIRM_DAYS": 2,
    "EARLY_FAIL_MA20_RATIO": 0.98,
    "EARLY_FAIL_CONFIRM_DAYS": 2,
    "EARLY_FAIL_RETURN_5": 0.00,
    "EARLY_PANIC_RETURN_5": -0.08,

    # V4-B: WEAK -> NEUTRAL (all GPIQ) only after bounce confirmation.
    "WEAK_RECOVER_MA20_RATIO": 1.00,
    "WEAK_RECOVER_RETURN_5": 0.00,
    "WEAK_RECOVER_CONFIRM_DAYS": 2,

    # Explicit substitution; do NOT interpret QQQ as covered-call income.
    "ACTUAL_AVAILABLE_FROM": {
        "GPIQ": (2023, 11, 1),
        "JEPI": (2020, 6, 1),
        "SGOV": (2020, 6, 1),
        "SPMO": (2015, 11, 1),
    },
    "PROXY_TICKER": {
        "GPIQ": "QQQ",   # growth total-return proxy, NOT income proxy
        "JEPI": "SPLV",  # lower-volatility equity, NO option overlay
        "SGOV": "BIL",   # short T-bills, related but not identical
    },

    # SPMO trend-signal experiment (new; default inactive). TILT's lower bound
    # for the SPMO share of the ATTACK sleeve when SPMO's own trend is weak;
    # the only new tunable this experiment introduces -- everything else
    # reuses the existing QQQ bull thresholds (BULL_MA200_RATIO/BULL_RETURN_63
    # /BEAR_MA200_RATIO/CONFIRM_DAYS) so this stays a single-factor change.
    "SPMO_TILT_MIN_WEIGHT": 0.0,

    # BEAR_LOGIC="RECOVERY_ONLY_IMPROVED" only (ORIGINAL never reads this;
    # the BEAR ENTRY side is left byte-identical to ORIGINAL in both modes --
    # see below for why). RECOVERY_RET63_MIN adds a second, independent
    # recovering path (ret63 back above this plus ratio>1.0) alongside
    # ORIGINAL's ratio-based one. Data-validated against two real episodes:
    # the real 2018-10-25 SPMO drawdown (genuine: entry-to-trough was -15%,
    # but ORIGINAL's recovering condition didn't confirm an exit until
    # 2019-02-15, ~3.5 months later -- the new path confirms ~1 week earlier,
    # 2019-02-07) and the real 2025-04-07 SPMO crash (the new path reproduces
    # the IDENTICAL confirmation date as ORIGINAL, 2025-05-15, so it never
    # pulls forward a genuine crash's recovery call). I.e. it only speeds up
    # a slow recovery, never a real one.
    #
    # A symmetric "DEPTH_CONFIRMED" entry-side idea (require a same-day dd126
    # floor before counting a shallow ratio/streak trigger as BEAR) was tried
    # and REJECTED after broader synthetic-data testing (not just the two
    # real episodes above): dd126 is a 126-day peak-to-trough measure that
    # lags a fast-moving ratio signal, so the depth floor delays entry more
    # the SLOWER a decline is, and for a genuinely slow ~-10% decline it
    # never confirms at all before the decline reverses on its own -- i.e.
    # the "confirmation" can silently never fire for exactly the kind of mild
    # drawdown it was meant to filter out. See test_state_machine.py's
    # test_depth_confirmed_does_not_delay_a_genuine_deep_crash-era test
    # history / this module's git log for the synthetic-crash sweep that
    # found it (an earlier prototype also tried adding BEAR_CONFIRM_DAYS
    # confirmation on top of the depth floor, which was even worse: it
    # delayed the real 2025-04-01 entry to 04-07, missing the crash's worst
    # two days and dropping DEV_RECENT adj_TWR from 109.5% to 75.7%). The
    # entry side is therefore NOT modified by any BEAR_LOGIC value.
    "RECOVERY_RET63_MIN": 0.0,

    # No-trade band (default 0.0 = current behavior: any nonzero drift from
    # target triggers a full order). A positive value skips an individual
    # ticker's order when its dollar size is under this fraction of NAV --
    # see engine.run_backtest()'s order loop.
    "NO_TRADE_BAND": 0.0,

    # Volatility-target sizing (VOL_TARGET_MODE="VOL_TARGET" only; these two
    # are fixed implementation details, not matrix dimensions, same role as
    # SPMO_TILT_MIN_WEIGHT). VOL_LOOKBACK_DAYS: realized-vol window for the
    # single largest-weight ticker in a risk-on regime's (ATTACK/NEUTRAL/
    # RECOVERY) role table. VOL_MIN_SCALE: floor on how far that ticker's
    # weight can be scaled down (never to zero, to avoid interacting badly
    # with the other weight/asset mechanisms by going flat) -- see
    # engine._vol_scale_weights().
    "VOL_LOOKBACK_DAYS": 20,
    "VOL_MIN_SCALE": 0.3,

    # ASSET_SWITCH_MODE="CONFIRM_N"/"MARGIN_BAND" only (ATTACK_ASSET="STRONGER"
    # sub-dimension suppressing SPMO<->QQQ daily whipsaw -- see
    # state_machine.resolve_attack_asset()). Fixed implementation details, not
    # matrix dimensions, same role as SPMO_TILT_MIN_WEIGHT/VOL_LOOKBACK_DAYS.
    "ASSET_SWITCH_CONFIRM_DAYS": 3,
    "ASSET_SWITCH_MARGIN": 0.01,
}

ALL_TICKERS = ("QQQ", "SPMO", "GPIQ", "JEPI", "SGOV", "SPLV", "BIL")


def assert_final_holdout_unlocked(run_mode, parameters_frozen, unlock_final_holdout):
    """The FINAL_HOLDOUT two-key lock, factored out of build_cfg() so other
    independent cfg-builders (e.g. config_v5.build_cfg_v5) can enforce the
    identical safety check without duplicating it. Behavior-identical to the
    inline check this replaced; see test_final_holdout_locked_by_default."""
    if run_mode == "FINAL_HOLDOUT" and not (parameters_frozen and unlock_final_holdout):
        raise ValueError(
            "FINAL_HOLDOUT IS LOCKED. Freeze parameters and explicitly pass "
            "both parameters_frozen=True and unlock_final_holdout=True only "
            "for the single final evaluation."
        )


def build_cfg(experiment, run_mode, parameters_frozen=False, unlock_final_holdout=False,
              trend_source="QQQ", spmo_mode=None, weight_scheme="ORIGINAL",
              bear_logic="ORIGINAL", attack_asset="SPMO", attack_asset_comparison="RATIO",
              overrides=None, no_trade_band=0.0, vol_target_mode="NONE",
              vol_target_annual=0.15, dividend_reinvest="DEFERRED",
              asset_switch_mode="DAILY"):
    """Returns a fresh CFG dict for one run, deep-copied from BASE_CFG.

    trend_source/spmo_mode default to the original QQQ-only behavior; passing
    either activates the SPMO trend-signal experiment (see
    state_machine.spmo_bull_signal/tilt_attack_weights and engine.run_backtest).

    weight_scheme selects a ROLE_WEIGHTS table from ROLE_WEIGHT_SCHEMES;
    "ORIGINAL" (default) is byte-for-byte the table ported from
    reference/SGS_V3_2_BASE_main.py CFG.

    bear_logic selects the BEAR recovery rule version; "ORIGINAL" (default)
    is the unmodified rule (see state_machine.step()). "RECOVERY_ONLY_IMPROVED"
    adds a data-validated second recovering path, with BEAR entry left
    byte-identical (see BASE_CFG's RECOVERY_RET63_MIN comment for the
    historical episodes it was checked against, and why an entry-side
    change was tried and rejected).

    attack_asset/attack_asset_comparison select what ATTACK/NEUTRAL/RECOVERY
    actually hold in place of a weight_scheme's hardcoded "SPMO" role:
    "SPMO" (default, unchanged), "QQQ" (swap to QQQ), or "STRONGER" (pick
    whichever of SPMO/QQQ is stronger each day, by "RATIO" or "RET20"; see
    state_machine.stronger_asset() and engine._swap_role()).

    overrides: optional dict of scalar threshold overrides, restricted to
    OVERRIDABLE_PARAMS -- a parameter-sensitivity probe, never a way to
    redefine ROLE_WEIGHTS/ACTUAL_AVAILABLE_FROM/other structural fields.

    no_trade_band: fraction of NAV below which an individual ticker's order
    is skipped (default 0.0 = current behavior, every nonzero drift trades).
    See engine.run_backtest()'s order loop.

    vol_target_mode/vol_target_annual: "NONE" (default, unchanged) or
    "VOL_TARGET" to scale down the single largest-weight ticker in a risk-on
    regime's (ATTACK/NEUTRAL/RECOVERY) role table when its own realized
    volatility exceeds vol_target_annual. See engine._vol_scale_weights().

    dividend_reinvest: "DEFERRED" (default, unchanged -- dividends sit as
    cash until the next rebalance or opportunistic buy) or "IMMEDIATE" (ex-
    date dividend cash is spent back toward current targets the same day,
    without the entry_ok gate). See engine.run_backtest()'s dividend handling.

    asset_switch_mode: only meaningful when attack_asset="STRONGER" (rejected
    otherwise). Suppresses SPMO<->QQQ daily whipsaw in "STRONGER"'s daily
    re-comparison: "DAILY" (default, unchanged -- today's stronger asset
    always wins), "CONFIRM_N" (a new winner must lead for
    ASSET_SWITCH_CONFIRM_DAYS consecutive days before the switch actually
    happens), "MARGIN_BAND" (a new winner must lead by more than
    ASSET_SWITCH_MARGIN, relative, before switching), or "LOCK_PER_EPISODE"
    (the asset is decided once on entering the ATTACK/NEUTRAL/RECOVERY group
    from outside it, then held fixed through any ATTACK<->NEUTRAL<->RECOVERY
    churn until the group is actually exited). See
    state_machine.resolve_attack_asset().
    """
    if experiment not in EXPERIMENTS:
        raise ValueError(f"EXPERIMENT must be one of {EXPERIMENTS}")
    if run_mode not in RUN_MODES:
        raise ValueError(f"RUN_MODE must be one of {RUN_MODES}")
    if trend_source not in TREND_SOURCES:
        raise ValueError(f"TREND_SOURCE must be one of {TREND_SOURCES}")
    if spmo_mode not in SPMO_MODES:
        raise ValueError(f"SPMO_MODE must be one of {SPMO_MODES}")
    if trend_source == "SPMO" and spmo_mode is not None:
        raise ValueError("SPMO cannot be both the sole trend source and its own gate/tilt/dual signal")
    if weight_scheme not in WEIGHT_SCHEMES:
        raise ValueError(f"WEIGHT_SCHEME must be one of {WEIGHT_SCHEMES}")
    if bear_logic not in BEAR_LOGICS:
        raise ValueError(f"BEAR_LOGIC must be one of {BEAR_LOGICS}")
    if attack_asset not in ATTACK_ASSETS:
        raise ValueError(f"ATTACK_ASSET must be one of {ATTACK_ASSETS}")
    if attack_asset_comparison not in ATTACK_ASSET_COMPARISONS:
        raise ValueError(f"ATTACK_ASSET_COMPARISON must be one of {ATTACK_ASSET_COMPARISONS}")
    if overrides:
        unknown = set(overrides) - OVERRIDABLE_PARAMS
        if unknown:
            raise ValueError(f"overrides contains non-overridable keys: {unknown}")
    if not 0 <= no_trade_band < 0.5:
        raise ValueError("no_trade_band must be in [0, 0.5)")
    if vol_target_mode not in VOL_TARGET_MODES:
        raise ValueError(f"VOL_TARGET_MODE must be one of {VOL_TARGET_MODES}")
    if vol_target_mode == "VOL_TARGET" and vol_target_annual <= 0:
        raise ValueError("vol_target_annual must be positive")
    if dividend_reinvest not in DIVIDEND_REINVEST_MODES:
        raise ValueError(f"DIVIDEND_REINVEST must be one of {DIVIDEND_REINVEST_MODES}")
    if asset_switch_mode not in ASSET_SWITCH_MODES:
        raise ValueError(f"ASSET_SWITCH_MODE must be one of {ASSET_SWITCH_MODES}")
    if asset_switch_mode != "DAILY" and attack_asset != "STRONGER":
        raise ValueError("asset_switch_mode other than DAILY only applies when attack_asset='STRONGER'")
    role_weights = ROLE_WEIGHT_SCHEMES[weight_scheme]
    if set(role_weights) != REQUIRED_REGIMES:
        raise ValueError(f"weight_scheme {weight_scheme!r} must define exactly {REQUIRED_REGIMES}")
    for name, weights in role_weights.items():
        if abs(sum(weights.values()) - (1 - BASE_CFG["CASH_BUFFER"])) > 1e-8:
            raise ValueError("Weights in " + name + " must sum to 1-CASH_BUFFER")
    if spmo_mode == "TILT" and not {"GPIQ", "SPMO"} <= set(role_weights["ATTACK"]):
        raise ValueError(f"SPMO_MODE=TILT requires both GPIQ and SPMO in ATTACK; weight_scheme {weight_scheme!r} lacks one")
    assert_final_holdout_unlocked(run_mode, parameters_frozen, unlock_final_holdout)
    cfg = copy.deepcopy(BASE_CFG)
    cfg["EXPERIMENT"] = experiment
    cfg["RUN_MODE"] = run_mode
    cfg["PARAMETERS_FROZEN"] = parameters_frozen
    cfg["UNLOCK_FINAL_HOLDOUT"] = unlock_final_holdout
    cfg["USE_EARLY"] = experiment in ("EARLY_ONLY", "BOTH")
    cfg["USE_WEAK"] = experiment in ("WEAK_ONLY", "BOTH")
    cfg["TREND_SOURCE"] = trend_source
    cfg["SPMO_MODE"] = spmo_mode
    cfg["WEIGHT_SCHEME"] = weight_scheme
    cfg["ROLE_WEIGHTS"] = copy.deepcopy(role_weights)
    cfg["BEAR_LOGIC"] = bear_logic
    cfg["ATTACK_ASSET"] = attack_asset
    cfg["ATTACK_ASSET_COMPARISON"] = attack_asset_comparison
    if overrides:
        cfg.update(overrides)
    cfg["OVERRIDES"] = dict(overrides or {})
    cfg["NO_TRADE_BAND"] = no_trade_band
    cfg["VOL_TARGET_MODE"] = vol_target_mode
    cfg["VOL_TARGET_ANNUAL"] = vol_target_annual
    cfg["DIVIDEND_REINVEST"] = dividend_reinvest
    cfg["ASSET_SWITCH_MODE"] = asset_switch_mode
    start, end = cfg["PERIODS"][run_mode]
    cfg["START"] = "%04d-%02d-%02d" % start
    cfg["END"] = "%04d-%02d-%02d" % end
    return cfg

"""Config for the V5 five-bucket continuous-allocation track (regime_v5.py /
engine_v5.py) -- a brand-new PARALLEL experiment, independent of config.py's
build_cfg()/BASE_CFG/ROLE_WEIGHT_SCHEMES.

Deliberately does NOT import ROLE_WEIGHT_SCHEMES, ACTUAL_AVAILABLE_FROM,
PROXY_TICKER, REQUIRED_REGIMES, OVERRIDABLE_PARAMS, or build_cfg itself --
build_cfg()'s validation (exactly-6-regime-names, weights-sum-to-1-CASH_BUFFER
per discrete table) is fundamentally shaped around the old track's discrete
weight-table model and cannot and should not be stretched to also validate a
5-bucket continuous-weight shape.

Only a handful of genuinely-shared, purely scalar execution-level constants
are read (read-only, never written back) straight out of config.BASE_CFG:
PERIODS, STARTING_CASH_USD, MONTHLY_DEPOSIT_USD, SLIPPAGE_PER_SIDE,
ANNUAL_RF_ASSUMPTION, CASH_BUFFER -- so these stay single-sourced rather than
duplicated and able to drift between the two tracks.
"""
from __future__ import annotations

import copy

from .config import ALL_TICKERS, BASE_CFG, assert_final_holdout_unlocked
from .regime_v5 import (BUCKET_MODES, CASH_RATIO_MODES, DERISK_ASSETS, DOWN_ASSETS,
                        STRENGTH_CLASSIFIER_MODES, STRONG_DOWN_ASSETS,
                        STRONG_SIGNAL_SOURCES, STRONG_UP_ASSETS, STRONG_UP_RULES,
                        UP_SLOT_MODES, V5_REGIMES_7)

RUN_MODES = ("DEV_EARLY", "DEV_RECENT", "FINAL_HOLDOUT")

# QLD (2x Nasdaq-100), BIL (1-3 month T-bills) and SGOV (0-3 month T-bills)
# are candidate replacement assets for the leveraged/inverse buckets. QLD and
# BIL listed in 2006/2007, well before DEV_EARLY's lookback; SGOV only listed
# 2020-06, so it is routed through BIL before then (see V5_PROXIES).
V5_EXTRA_TICKERS = ("TQQQ", "PSQ", "QID", "QLD", "BIL", "SGOV", "QQQM")

# V5 holds QQQM (Invesco's lower-fee Nasdaq-100 share class) wherever it would
# otherwise hold QQQ. QQQM only listed 2020-10-13, so before then the position
# is routed through QQQ itself (V5_PROXIES), and the trend signal is QQQ's
# adjusted history stitched to QQQM's from its first full day (see
# data.load_stitched_signal_series). The old 6-regime track still uses QQQ.
V5_NDX_TICKER = "QQQM"
V5_NDX_LISTED_FROM = (2020, 10, 13)

# Full bar universe to load for V5 runs. BIL is already in config.ALL_TICKERS,
# so plain concatenation would load it twice and duplicate its bars --
# always use this deduplicated tuple instead of ALL_TICKERS + V5_EXTRA_TICKERS.
V5_BAR_SYMBOLS = tuple(dict.fromkeys(ALL_TICKERS + V5_EXTRA_TICKERS))

# GPIQ itself isn't listed until 2023-11-01 (confirmed against the cached
# data), but every V5 bucket holds a GPIQ floor across all three periods,
# including DEV_EARLY (2016-2019) and FINAL_HOLDOUT (2020-2022) -- both
# entirely before GPIQ existed, so it needs a pre-listing substitute. The
# listing date is read from config.BASE_CFG so it stays single-sourced; the
# substitute itself is V5's Nasdaq-100 ticker (see V5_PROXIES).
GPIQ_AVAILABLE_FROM = BASE_CFG["ACTUAL_AVAILABLE_FROM"]["GPIQ"]

# Every V5 ticker with a pre-listing gap: {ticker: (listed_from, proxy)}.
# SGOV (listed 2020-06) uses BIL before then -- the same pair and date the
# old track already uses, read from config.BASE_CFG so it stays single-sourced.
# GPIQ's proxy is V5's Nasdaq-100 ticker (QQQM) rather than the old track's
# QQQ; before QQQM listed, regime_v5.effective_targets_v5 follows the chain one
# more step (GPIQ -> QQQM -> QQQ), so GPIQ ends up in whichever is tradeable.
V5_PROXIES = {
    "GPIQ": (GPIQ_AVAILABLE_FROM, V5_NDX_TICKER),
    "SGOV": (BASE_CFG["ACTUAL_AVAILABLE_FROM"]["SGOV"], BASE_CFG["PROXY_TICKER"]["SGOV"]),
    V5_NDX_TICKER: (V5_NDX_LISTED_FROM, "QQQ"),
}

# Whitelist for build_cfg_v5(overrides=...): plain scalar thresholds only.
OVERRIDABLE_PARAMS_V5 = {
    "V5_UP_RATIO", "V5_DOWN_RATIO", "V5_CONFIRM_DAYS", "V5_DOWN_CONFIRM_DAYS",
    "V5_STRONG_UP_RATIO", "V5_STRONG_UP_RET63",
    "V5_STRONG_DOWN_RATIO", "V5_STRONG_DOWN_RET63",
    "V5_STRONG_PERCENTILE", "V5_WEAK_PERCENTILE", "V5_PERCENTILE_LOOKBACK",
    "V5_SPMO_ENTRY_RATIO", "V5_SPMO_EXIT_RATIO",
    "V5_GPIQ_FLOOR", "V5_UP_GPIQ_MAX", "V5_UP_GPIQ_MIN",
    "V5_VOL_TARGET_ANNUAL", "V5_VOL_LOOKBACK_DAYS", "V5_CASH_MIN_SCALE",
    "V5_SIGNAL_SATURATION_BAND",
    "V5_CHOPPY_UP_RATIO", "V5_CHOPPY_DOWN_RATIO", "V5_CHOPPY_ER_LOOKBACK", "V5_CHOPPY_ER_MAX",
    "V5_STRONG_ER_LOOKBACK", "V5_STRONG_ER_MIN", "V5_STRONG_ER_EXIT", "V5_STRONG_MIN_HOLD",
    "V5_VOLSPIKE_MULT",
    "V5_CASH_BUFFER", "V5_CASH_RESERVE_USD", "V5_REDEPLOY_FREED_CASH", "V5_UP_EXIT_RATIO",
    "V5_STRONG_AFTER_DAYS", "V5_CHOP_CROSSINGS", "V5_CHOP_SCOPE",
}

BASE_CFG_V5 = {
    # What STRONG_UP / DOWN / STRONG_DOWN hold (see regime_v5.bucket_risk_ticker
    # and the *_ASSETS tuples for the allowed values). Not in
    # OVERRIDABLE_PARAMS_V5 because they're categorical, not numeric thresholds;
    # set them through build_cfg_v5's strong_up_asset/down_asset/
    # strong_down_asset arguments instead.
    #
    # SPMO/BIL/BIL replaced the original TQQQ/PSQ/QID. On the days V5 actually
    # assigned to these buckets, QQQ itself barely moved during STRONG_UP
    # (leverage only amplified noise: TQQQ ranked last of 5 STRONG_UP choices)
    # and mostly ROSE during DOWN/STRONG_DOWN (the down signal fires near
    # short-term lows, so PSQ/QID lost money). A full-engine matrix (17 asset
    # combos x 2 classifiers x 2 dev periods, VOL_TARGET) ranked SPMO+GPIQ/GPIQ
    # first by average Sharpe (1.055) but with a -22.2% worst MaxDD;
    # SPMO+BIL/BIL was effectively tied (1.049) with a -15.9% worst MaxDD, and
    # was chosen for the drawdown protection. Versus TQQQ/PSQ/QID it improved
    # Sharpe and MaxDD in all 8 classifier x cash-mode x period combinations
    # (average Sharpe 0.644 -> 1.049 under VOL_TARGET).
    #
    # BIL was then swapped for SGOV (0-3 month T-bills, routed through BIL before
    # its 2020-06 listing via V5_PROXIES): identical results (1.049 vs 1.049).
    # Follow-up full-engine checks (22 combos x 2 classifiers x 2 periods):
    #   - DOWN=PSQ (plain -1x short, no leverage) with STRONG_DOWN=SGOV averaged
    #     1.001 vs 1.049 for SGOV/SGOV: slightly better on DEV_RECENT (1.428 vs
    #     1.420) but clearly worse on DEV_EARLY (0.494 vs 0.661). Kept available.
    #   - STRONG_UP=SPMO_SLOT (same SPMO-or-QQQ pick as UP) averaged 1.037 and
    #     STRONG_UP=QQQ 0.945, vs 1.049 for fixed SPMO.
    #
    # STRONG_UP went back to TQQQ once STRONG_UP itself was redefined (the
    # EFFICIENCY rule below): TQQQ was a poor choice under the old THRESHOLD
    # strong-up call, but on days whose last 20 sessions rose in a near-
    # straight line it earned its leverage. Config "E" (TQQQ + EFFICIENCY with
    # exit 0.25 + UP slot EQUAL + 5% rebalance band), same classifier for both:
    #   DEV_EARLY  +98.9% / MaxDD -19.9% / Sharpe 0.789  (was +61.2 / -15.8 / 0.695)
    #   DEV_RECENT +139.5% / MaxDD -17.9% / Sharpe 1.218 (was +98.9 / -12.1 / 1.421)
    # Higher return and DEV_EARLY Sharpe for a deeper drawdown; chosen by the
    # user over the previous SPMO default.
    #
    # (Configs E / F / F2 below were later replaced by config G -- see
    # V5_STRONG_UP_RULE -- after their strong-up rule failed out of sample.)
    #
    # Config "F" then tuned E's UP / STRONG_UP (sweeps of ~400 configs, dev
    # periods only): V5_STRONG_ER_MIN 0.35 -> 0.40, V5_UP_RATIO 1.03 -> 1.04,
    # V5_CASH_BUFFER 2% -> 0, and the slice the volatility target trims off
    # parked in SGOV (UP) / QQQM (STRONG_UP) instead of idle cash:
    #   DEV_EARLY  +113.6% / MaxDD -19.8% / Sharpe 0.889
    #   DEV_RECENT +166.1% / MaxDD -16.6% / Sharpe 1.369
    # Better than E on return, drawdown and Sharpe in both periods, and idle
    # cash in UP / STRONG_UP fell from 5.5% / 12.7% of NAV to 1.1% / 1.8%.
    "V5_STRONG_UP_ASSET": "TQQQ",
    "V5_DOWN_ASSET": "SGOV",
    "V5_STRONG_DOWN_ASSET": "SGOV",

    # Shared execution-level constants, read-only from config.BASE_CFG.
    "PERIODS": copy.deepcopy(BASE_CFG["PERIODS"]),
    "STARTING_CASH_USD": BASE_CFG["STARTING_CASH_USD"],
    "MONTHLY_DEPOSIT_USD": BASE_CFG["MONTHLY_DEPOSIT_USD"],
    "SLIPPAGE_PER_SIDE": BASE_CFG["SLIPPAGE_PER_SIDE"],
    "ANNUAL_RF_ASSUMPTION": BASE_CFG["ANNUAL_RF_ASSUMPTION"],
    # Kept idle at every rebalance. The old track's 2% (config.BASE_CFG);
    # V5 uses 0 (config F) -- whole-share rounding already leaves ~1% idle.
    "V5_CASH_BUFFER": 0.0,
    "GPIQ_AVAILABLE_FROM": GPIQ_AVAILABLE_FROM,
    "V5_NDX_TICKER": V5_NDX_TICKER,
    "V5_PROXIES": copy.deepcopy(V5_PROXIES),

    # 3-way base classification (QQQ's own trend only). V5_UP_RATIO 1.02
    # (config G). Under the ALL_UP strong-up rule every up-family day holds
    # TQQQ, so this ratio is the leverage switch. On the 2000-2026 replica
    # (research/v5_long_replica.py) 1.01-1.02 sit on a flat plateau
    # (out-of-sample Sharpe 0.24-0.28, 15-17 of 24 years beating QQQ), with
    # 1.025-1.03 lower and no isolated peak. (Config F used 1.04 with the
    # EFFICIENCY rule, where 1.01-1.02 were worse.)
    #
    # Config G2 (current default): entry 1.035 with the exit band below at
    # 1.01. Chosen on 2000-2019 + 2023-2026 only (2020-2022 already seen):
    # CAGR 16.8% vs G's 14.5%, average Sharpe 0.671 vs 0.624, choppy years
    # (2004-06, 2011, 2015-16, 2018) -4.5%/yr vs -9.3%, same 18 years beating
    # QQQ. Entry 1.03-1.045 with exit 1.01 is a flat plateau. The exit band
    # does most of the work (entry 1.02 / exit 1.01 already gives 17.7% vs
    # 16.1% CAGR over 2000-2026); raising entry only helps together with it
    # (entry 1.035 / exit 1.02 was no better than G).
    "V5_UP_RATIO": 1.035,
    "V5_DOWN_RATIO": 0.97,
    # Up-family exit band (None = off): stay in UP / STRONG_UP until the ratio
    # falls below this instead of below V5_UP_RATIO. 1.01 in config G2.
    "V5_UP_EXIT_RATIO": 1.01,
    # Optional seasoning (0 = off): STRONG_UP only after the up-family has
    # been confirmed for more than this many days; before that it is UP.
    "V5_STRONG_AFTER_DAYS": 0,
    # Optional choppy-market master switch (0 = off): see regime_v5.step_v5.
    "V5_CHOP_CROSSINGS": 0,
    "V5_CHOP_SCOPE": "UP",

    # Seven-level split (regime_v5.BUCKET_MODES). FIVE keeps the original
    # STRONG_UP/UP/SIDEWAYS/DOWN/STRONG_DOWN. RATIO_BAND carves CHOPPY_UP /
    # CHOPPY_DOWN out of the SIDEWAYS band at +/-1% from QQQ's MA200;
    # EFFICIENCY demotes a confirmed UP/DOWN to CHOPPY when QQQ's 20-day
    # efficiency ratio is below V5_CHOPPY_ER_MAX.
    #
    # FIVE stays the default: in a full-engine comparison (same SPMO/SGOV/SGOV
    # assets, 2 classifiers x 2 periods, VOL_TARGET) both seven-level modes did
    # worse -- average Sharpe 1.049 (FIVE) vs 0.885 (RATIO_BAND) vs 0.855
    # (EFFICIENCY). RATIO_BAND's +/-1% band sits right where QQQ's ratio
    # oscillates, nearly doubling state switches (55 -> 102 on DEV_EARLY) and
    # trimming GPIQ into SGOV just before rebounds; EFFICIENCY relabels ~60% of
    # profitable UP days as choppy and nearly triples fees ($346 -> $896).
    "V5_BUCKET_MODE": "FIVE",
    "V5_CHOPPY_UP_RATIO": 1.01,
    "V5_CHOPPY_DOWN_RATIO": 0.99,
    "V5_CHOPPY_ER_LOOKBACK": 20,
    "V5_CHOPPY_ER_MAX": 0.25,

    # UP / CHOPPY_UP risk slot (regime_v5.UP_SLOT_MODES): SPMO_GATED (original:
    # SPMO while its own trend qualifies, else QQQM), EQUAL, or TREND. EQUAL
    # (part of config E, see V5_STRONG_UP_ASSET) holds both all the time.
    "V5_UP_SLOT_MODE": "EQUAL",

    # How STRONG_UP is decided (regime_v5.STRONG_UP_RULES) and the EFFICIENCY
    # rule's parameters.
    #
    # Config G: ALL_UP -- every confirmed up-family day is STRONG_UP, i.e. no
    # fitted strength test; how much TQQQ is actually held is left to the
    # volatility target (3 x 15% = 45%/yr) and the CASCADE below it.
    # Chosen on 2000-2026 with V5's decision code replayed on stand-in assets
    # (research/strong_up_candidates*.py), FINAL_HOLDOUT excluded:
    #   - EFFICIENCY (config F/F2) was strong on the dev periods but lowered
    #     Sharpe on 2000-2015 for every lookback 10-40 / entry 0.30-0.45
    #     (research/strong_up_oos.py) -- fitted to recent years.
    #   - Fixed-window rules (MA stack, 1/3/6/12-month momentum, near the
    #     52-week high, calm trend, votes of these, graded TQQQ share) and
    #     ALL_UP were compared with TQQQ / QLD / unlevered holdings.
    #   - Pre-set pick: beat both F2 and QQQ on 2000-2015 Sharpe, then the
    #     highest long-run return. ALL_UP + TQQQ + UP_RATIO 1.02:
    #     2000-2015 Sharpe 0.25 (F2 0.06, QQQ 0.14); 2000-2026 CAGR 14.5%
    #     (QQQ 7.7%); beat QQQ in 18 of 24 calendar years (F2 13). Real
    #     engine: DEV_EARLY +210% / -27.9% / 0.964, DEV_RECENT +188% /
    #     -27.9% / 1.043 (QQQ DCA +101% / -22.7% / 0.885 and +104% / -22.5% /
    #     1.101). Cost: deeper drawdowns than QQQ DCA, a lower Sharpe than
    #     DCA on DEV_RECENT, and losses in choppy years around the MA200
    #     (2004-05, 2011, 2015: leverage bought into every failed breakout).
    #     ALL_UP + QLD is the lower-drawdown variant (-22%, 15 of 24 years).
    #   - Cutting STRONG_UP's drawdown (2026-10-20, same 2000-2026 replica +
    #     real engine): strong_up_asset="TQQQ_QLD" (half each) was the best
    #     trade-off -- still 18 of 24 years beating QQQ, CAGR 13.2% vs 14.5%,
    #     dev MaxDD -24.3% / -25.0% vs -27.2% / -28.4%. A lower vol target
    #     (12%) cut less drawdown per point of return; de-levering when the
    #     price falls below its 20/50/100-day average whipsawed (CAGR 4.5-8%);
    #     a slow volatility window (max of 20d and 60-126d) changed little.
    "V5_STRONG_UP_RULE": "ALL_UP",
    # V5_STRONG_ER_MIN 0.40 (config F, was 0.35). Sweep over lookback 10-40 x
    # entry 0.30-0.50 x exit 0.15-0.30: entry 0.38-0.42 with exit 0.22-0.28
    # all beat 0.35 at lookback 20. Lookback 20 itself is a sharp peak (18 and
    # 22 average ~0.1-0.15 lower Sharpe), so treat this as the most fragile
    # parameter. Things tried and NOT adopted: averaging the efficiency ratio
    # over several lookbacks (more stable but lower overall), requiring 2-4
    # qualifying days before entry (worse every time), and a trailing stop
    # (3-8% off the 20-day high: never fired -- the efficiency ratio already
    # exits first).
    "V5_STRONG_ER_LOOKBACK": 20,
    "V5_STRONG_ER_MIN": 0.40,
    # EFFICIENCY rule only: stay in STRONG_UP while er >= V5_STRONG_ER_EXIT
    # (hysteresis; equal to ER_MIN means none), and for at least
    # V5_STRONG_MIN_HOLD days once entered (0 = no minimum). Without the
    # hysteresis a third of the strong-up episodes lasted 1-2 days.
    "V5_STRONG_ER_EXIT": 0.25,
    "V5_STRONG_MIN_HOLD": 0,
    # Which price series the EFFICIENCY strong-up rule reads
    # (regime_v5.STRONG_SIGNAL_SOURCES). NDX = the Nasdaq-100 signal.
    "V5_STRONG_SIGNAL_SOURCE": "NDX",

    # Optional early-warning DOWN trigger: "NONE" (off) or "VOLSPIKE"
    # (20-day vol > V5_VOLSPIKE_MULT x its 252-day median and price below its
    # 50-day average -> DOWN). See regime_v5.volatility_spike().
    "V5_EARLY_DOWN": "NONE",
    "V5_VOLSPIKE_MULT": 1.5,

    # Buckets exempt from the cash/equity scaling (stay fully invested).
    "V5_FULL_INVEST_BUCKETS": (),
    # Where UP / STRONG_UP park the slice the cash/equity scaling trims off
    # (regime_v5.DERISK_ASSETS): CASH leaves it idle (the original); SGOV,
    # GPIQ or NDX (QQQM) keep it invested; CASCADE steps down level by level
    # (regime_v5.derisk_levels). UP -> SGOV (earns the T-bill rate, keeps the
    # de-risking). Putting UP's slice back into equity -- GPIQ (before its
    # 2023-11 listing that is really QQQ), QQQM, CASCADE (GPIQ then SGOV), or
    # V5_FULL_INVEST_BUCKETS -- helped DEV_EARLY slightly but deepened
    # DEV_RECENT's drawdown (-16.6% -> -18.1..-18.5%); average Sharpe fell
    # from 1.129 to 1.07-1.09.
    # STRONG_UP -> CASCADE: the slice TQQQ's volatility target trims off goes
    # first into UP's SPMO/QQQM sleeve, then GPIQ, each as far as its own
    # volatility target allows, then SGOV. On par with parking it all in QQQM
    # (config F's NDX): DEV_EARLY +114.2% / -19.7% / 0.894 vs +113.6% /
    # -19.8% / 0.889, DEV_RECENT +164.8% / -16.5% / 1.365 vs +166.1% / -16.6% /
    # 1.369; chosen because it de-levers one level at a time.
    "V5_UP_DERISK_ASSET": "SGOV",
    "V5_STRONG_DERISK_ASSET": "CASCADE",
    # Fixed idle-cash reserve in dollars on top of V5_CASH_BUFFER's fraction:
    # 13 USD (about 20 AUD) to cover fees. Leftover cash beyond that comes
    # from whole-share rounding and from the rebalance band holding back the
    # monthly deposit until it is worth an order (F averaged 1.3-1.6% idle).
    # A sweep that bought shares with any spare cash above the reserve cut
    # idle cash to 0.2% but cost DEV_RECENT ~5 points of return (Sharpe
    # 1.369 -> 1.327, +160 small orders), so it was not kept.
    "V5_CASH_RESERVE_USD": 13.0,
    # With the rebalance band, a trim split across several destinations (TQQQ
    # trimmed by its vol target, spread over the CASCADE levels) leaves every
    # piece below the band, so the proceeds sat idle -- under config G, cash
    # was above 5% of NAV on 125 of 744 DEV_RECENT days (8.7% on the last
    # day). True lets buys spend what that day's sells freed even when below
    # the band: idle cash fell to 1.0% on average (0 days above 5%) with
    # return / MaxDD / Sharpe unchanged (DEV_EARLY 0.960 vs 0.964, DEV_RECENT
    # 1.043 vs 1.043).
    "V5_REDEPLOY_FREED_CASH": True,

    # Rebalance dead band: skip an order whose dollar size is below this
    # fraction of NAV (0.0 = always trade, the original behaviour). Orders that
    # close a position out completely always execute, so no stub lots remain.
    # 5% is part of config E.
    "V5_REBALANCE_BAND": 0.05,
    # V5_CONFIRM_DAYS (UP-family only: up/strong_up -- see V5_DOWN_CONFIRM_DAYS
    # below for the DOWN-family's own, independent value) =4 (was 2): chosen
    # via the --v5-sensitivity-matrix sweep (results/results_dev_v5_sensitivity.csv)
    # to fix a confirmed UP<->STRONG_UP whipsaw -- a real 2-day SIDEWAYS->
    # STRONG_UP->SIDEWAYS flicker on 2016-06-08/09 forced a round-trip
    # liquidation into TQQQ right before that month's worst drawdown. 4 was
    # the single best-performing value in the sweep under BOTH classifier_modes
    # (avg Sharpe across DEV_EARLY+DEV_RECENT: 0.610 vs. 0.522 at the old
    # default 2); 1 is markedly worse (0.453, more whipsaw) and 5-6 overshoot
    # into instability (switches drop further but Sharpe also falls and MaxDD
    # worsens on at least one period -- 6 was the single worst-MaxDD
    # perturbation of the whole sweep at -29.7%). Verified jointly with
    # V5_STRONG_UP_RATIO=1.06 below (no negative interaction -- see that
    # comment) and confirmed to eliminate the 2016-06-08/09 flicker under
    # both classifier_modes.
    #
    # IMPORTANT: at the time this was raised from 2 to 4, V5_CONFIRM_DAYS
    # was a single value shared by all four streak labels (up/down/strong_up/
    # strong_down). Raising it fixed the UP-side whipsaw above but had an
    # UNINTENDED side effect on the DOWN side: during the real 2018-10/11 QQQ
    # correction, ratio oscillated around V5_DOWN_RATIO repeatedly, and
    # confirm_days=4 delayed DOWN confirmation from 2018-10-31 to 2018-11-23
    # (23 extra days held nearly fully invested in GPIQ while QQQ itself fell
    # -6.25%, directly dragging down DEV_EARLY's results). V5_DOWN_CONFIRM_DAYS
    # was split out specifically to undo that side effect without touching
    # this (UP-side) value, which remains correct at 4.
    "V5_CONFIRM_DAYS": 4,
    # V5_DOWN_CONFIRM_DAYS (DOWN-family only: down/strong_down) =2: restores
    # the pre-whipsaw-fix confirmation speed specifically for the downside,
    # asymmetric risk management (fast exit, patient entry) -- a standard
    # principle in live trading systems, and a direct extension of the
    # already-adopted asymmetric STRONG_UP boundary design. Verified via a
    # 1/2/3/4 sweep isolated to just this parameter (V5_CONFIRM_DAYS held at
    # 4): 2 was the best or tied-best value under both classifier_modes on
    # BOTH dev periods (DEV_EARLY Sharpe: THRESHOLD_EXT 0.427 vs. 0.394 at 4,
    # MOMENTUM_VOL_PERCENTILE 0.314 vs. 0.309 at 4; DEV_RECENT Sharpe only
    # ~0.02-0.004 below the value at 4, MaxDD essentially unchanged) --
    # confirmed via the engine-level real-data regression test that it
    # measurably speeds up exiting SIDEWAYS during the real 2018-10/11
    # correction versus the un-split confirm_days=4 behavior.
    "V5_DOWN_CONFIRM_DAYS": 2,

    # THRESHOLD_EXT strength sub-classification.
    # V5_STRONG_UP_RATIO=1.06 (was 1.08): also chosen via the sensitivity
    # sweep. Counter-intuitively NARROWING (not widening) the UP->STRONG_UP
    # gap was the best-performing single perturbation under THRESHOLD_EXT
    # (avg Sharpe 0.515 vs. 0.437 baseline) and the second-best under
    # MOMENTUM_VOL_PERCENTILE (0.567) -- the ticker-aware vol-target fix
    # (V5_TICKER_LEVERAGE_MULTIPLIER, see regime_v5.py) made genuinely
    # capturing STRONG_UP's TQQQ exposure much more valuable than before,
    # so catching real strong trends sooner now outweighs the extra noise
    # sensitivity, PROVIDED V5_CONFIRM_DAYS=4 is also raised to filter that
    # noise out -- verified via a joint run (both changes together) showing
    # a materially higher avg Sharpe (0.561 THRESHOLD_EXT / 0.721
    # MOMENTUM_VOL_PERCENTILE) than either lever alone, confirming no
    # destructive interaction between the two.
    "V5_STRONG_UP_RATIO": 1.06,
    "V5_STRONG_UP_RET63": 0.10,
    "V5_STRONG_DOWN_RATIO": 0.90,
    "V5_STRONG_DOWN_RET63": -0.15,

    # MOMENTUM_VOL_PERCENTILE strength sub-classification.
    "V5_PERCENTILE_LOOKBACK": 252,
    "V5_STRONG_PERCENTILE": 80.0,
    "V5_WEAK_PERCENTILE": 20.0,

    # UP bucket's internal SPMO-vs-QQQ slot (judged on SPMO's own trend only).
    "V5_SPMO_ENTRY_RATIO": 1.03,
    "V5_SPMO_EXIT_RATIO": 0.99,

    # GPIQ floor + UP-bucket continuous tilt.
    "V5_GPIQ_FLOOR": 0.20,
    "V5_UP_GPIQ_MAX": 0.70,
    "V5_UP_GPIQ_MIN": 0.20,

    # Cash/equity dynamic ratio.
    "V5_CASH_MIN_SCALE": 0.3,
    "V5_VOL_TARGET_ANNUAL": 0.15,
    "V5_VOL_LOOKBACK_DAYS": 20,
    "V5_SIGNAL_SATURATION_BAND": 0.05,
}


def build_cfg_v5(run_mode, parameters_frozen=False, unlock_final_holdout=False,
                  classifier_mode="THRESHOLD_EXT", cash_ratio_mode="VOL_TARGET",
                  overrides=None, strong_up_asset=None, down_asset=None,
                  strong_down_asset=None, bucket_mode=None, up_slot_mode=None,
                  strong_up_rule=None, full_invest_buckets=None, rebalance_band=None,
                  strong_signal_source=None, early_down=None, up_derisk_asset=None,
                  strong_derisk_asset=None):
    """Returns a fresh cfg_v5 dict for one V5 run, deep-copied from
    BASE_CFG_V5. Mirrors config.build_cfg()'s shape (validation, then
    deep-copy, then stamp the run's own selections) but is an entirely
    separate function/dict-shape -- see this module's docstring for why.

    strong_up_asset/down_asset/strong_down_asset: None keeps BASE_CFG_V5's
    default for that bucket; otherwise must be one of regime_v5's
    STRONG_UP_ASSETS/DOWN_ASSETS/STRONG_DOWN_ASSETS."""
    if run_mode not in RUN_MODES:
        raise ValueError(f"RUN_MODE must be one of {RUN_MODES}")
    if classifier_mode not in STRENGTH_CLASSIFIER_MODES:
        raise ValueError(f"STRENGTH_CLASSIFIER_MODE must be one of {STRENGTH_CLASSIFIER_MODES}")
    if cash_ratio_mode not in CASH_RATIO_MODES:
        raise ValueError(f"CASH_RATIO_MODE must be one of {CASH_RATIO_MODES}")
    for value, allowed, name in ((strong_up_asset, STRONG_UP_ASSETS, "V5_STRONG_UP_ASSET"),
                                 (down_asset, DOWN_ASSETS, "V5_DOWN_ASSET"),
                                 (strong_down_asset, STRONG_DOWN_ASSETS, "V5_STRONG_DOWN_ASSET")):
        if value is not None and value not in allowed:
            raise ValueError(f"{name} must be one of {allowed}")
    if bucket_mode is not None and bucket_mode not in BUCKET_MODES:
        raise ValueError(f"V5_BUCKET_MODE must be one of {BUCKET_MODES}")
    if up_slot_mode is not None and up_slot_mode not in UP_SLOT_MODES:
        raise ValueError(f"V5_UP_SLOT_MODE must be one of {UP_SLOT_MODES}")
    if strong_up_rule is not None and strong_up_rule not in STRONG_UP_RULES:
        raise ValueError(f"V5_STRONG_UP_RULE must be one of {STRONG_UP_RULES}")
    if full_invest_buckets is not None:
        unknown = set(full_invest_buckets) - set(V5_REGIMES_7)
        if unknown:
            raise ValueError(f"V5_FULL_INVEST_BUCKETS has unknown buckets {sorted(unknown)}")
    if rebalance_band is not None and not 0.0 <= rebalance_band < 0.5:
        raise ValueError("V5_REBALANCE_BAND must be in [0, 0.5)")
    if strong_signal_source is not None and strong_signal_source not in STRONG_SIGNAL_SOURCES:
        raise ValueError(f"V5_STRONG_SIGNAL_SOURCE must be one of {STRONG_SIGNAL_SOURCES}")
    if early_down is not None and early_down not in ("NONE", "VOLSPIKE"):
        raise ValueError("V5_EARLY_DOWN must be one of ('NONE', 'VOLSPIKE')")
    for value, name in ((up_derisk_asset, "V5_UP_DERISK_ASSET"), (strong_derisk_asset, "V5_STRONG_DERISK_ASSET")):
        if value is not None and value not in DERISK_ASSETS:
            raise ValueError(f"{name} must be one of {DERISK_ASSETS}")
    if overrides:
        unknown = set(overrides) - OVERRIDABLE_PARAMS_V5
        if unknown:
            raise ValueError(f"overrides contains non-overridable keys: {unknown}")
    assert_final_holdout_unlocked(run_mode, parameters_frozen, unlock_final_holdout)

    cfg = copy.deepcopy(BASE_CFG_V5)
    cfg["RUN_MODE"] = run_mode
    cfg["PARAMETERS_FROZEN"] = parameters_frozen
    cfg["UNLOCK_FINAL_HOLDOUT"] = unlock_final_holdout
    cfg["STRENGTH_CLASSIFIER_MODE"] = classifier_mode
    cfg["CASH_RATIO_MODE"] = cash_ratio_mode
    if strong_up_asset is not None:
        cfg["V5_STRONG_UP_ASSET"] = strong_up_asset
    if down_asset is not None:
        cfg["V5_DOWN_ASSET"] = down_asset
    if strong_down_asset is not None:
        cfg["V5_STRONG_DOWN_ASSET"] = strong_down_asset
    if bucket_mode is not None:
        cfg["V5_BUCKET_MODE"] = bucket_mode
    if up_slot_mode is not None:
        cfg["V5_UP_SLOT_MODE"] = up_slot_mode
    if strong_up_rule is not None:
        cfg["V5_STRONG_UP_RULE"] = strong_up_rule
    if full_invest_buckets is not None:
        cfg["V5_FULL_INVEST_BUCKETS"] = tuple(full_invest_buckets)
    if rebalance_band is not None:
        cfg["V5_REBALANCE_BAND"] = float(rebalance_band)
    if strong_signal_source is not None:
        cfg["V5_STRONG_SIGNAL_SOURCE"] = strong_signal_source
    if early_down is not None:
        cfg["V5_EARLY_DOWN"] = early_down
    if up_derisk_asset is not None:
        cfg["V5_UP_DERISK_ASSET"] = up_derisk_asset
    if strong_derisk_asset is not None:
        cfg["V5_STRONG_DERISK_ASSET"] = strong_derisk_asset
    if overrides:
        cfg.update(overrides)
    cfg["OVERRIDES"] = dict(overrides or {})
    start, end = cfg["PERIODS"][run_mode]
    cfg["START"] = "%04d-%02d-%02d" % start
    cfg["END"] = "%04d-%02d-%02d" % end
    return cfg

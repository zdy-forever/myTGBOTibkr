"""Unit tests for config_v5.build_cfg_v5()'s validation and defaults -- a
pure config-layer concern, kept separate from test_regime_v5.py the same way
test_config.py is kept separate from test_state_machine.py for the old
track's analogous experiment dimensions.
"""
import pytest

from sgs.config_v5 import OVERRIDABLE_PARAMS_V5, build_cfg_v5


def test_build_cfg_v5_rejects_unknown_run_mode():
    with pytest.raises(ValueError, match="RUN_MODE"):
        build_cfg_v5("NOT_A_MODE")


def test_build_cfg_v5_rejects_unknown_classifier_mode():
    with pytest.raises(ValueError, match="STRENGTH_CLASSIFIER_MODE"):
        build_cfg_v5("DEV_EARLY", classifier_mode="BOGUS")


def test_build_cfg_v5_rejects_unknown_cash_ratio_mode():
    with pytest.raises(ValueError, match="CASH_RATIO_MODE"):
        build_cfg_v5("DEV_EARLY", cash_ratio_mode="BOGUS")


def test_build_cfg_v5_rejects_non_overridable_params():
    with pytest.raises(ValueError, match="non-overridable"):
        build_cfg_v5("DEV_EARLY", overrides={"PERIODS": {}})


def test_build_cfg_v5_overrides_apply_whitelisted_params():
    cfg = build_cfg_v5("DEV_EARLY", overrides={"V5_UP_RATIO": 1.05})
    assert cfg["V5_UP_RATIO"] == 1.05
    assert cfg["OVERRIDES"] == {"V5_UP_RATIO": 1.05}


def test_build_cfg_v5_every_whitelisted_param_is_individually_settable():
    """Every name in OVERRIDABLE_PARAMS_V5 must actually exist in
    BASE_CFG_V5 and be settable without error -- catches a typo in the
    whitelist itself (same pattern as test_config.py's analogous test)."""
    for param in OVERRIDABLE_PARAMS_V5:
        cfg = build_cfg_v5("DEV_EARLY", overrides={param: 999})
        assert cfg[param] == 999


def test_build_cfg_v5_defaults():
    cfg = build_cfg_v5("DEV_EARLY")
    assert cfg["STRENGTH_CLASSIFIER_MODE"] == "THRESHOLD_EXT"
    assert cfg["CASH_RATIO_MODE"] == "VOL_TARGET"
    assert cfg["OVERRIDES"] == {}


def test_build_cfg_v5_final_holdout_locked_by_default():
    with pytest.raises(ValueError, match="LOCKED"):
        build_cfg_v5("FINAL_HOLDOUT")
    with pytest.raises(ValueError, match="LOCKED"):
        build_cfg_v5("FINAL_HOLDOUT", parameters_frozen=True, unlock_final_holdout=False)
    with pytest.raises(ValueError, match="LOCKED"):
        build_cfg_v5("FINAL_HOLDOUT", parameters_frozen=False, unlock_final_holdout=True)
    cfg = build_cfg_v5("FINAL_HOLDOUT", parameters_frozen=True, unlock_final_holdout=True)
    assert cfg["RUN_MODE"] == "FINAL_HOLDOUT"


def test_build_cfg_v5_start_end_match_periods():
    cfg = build_cfg_v5("DEV_EARLY")
    assert cfg["START"] == "2016-01-04"
    assert cfg["END"] == "2019-12-31"

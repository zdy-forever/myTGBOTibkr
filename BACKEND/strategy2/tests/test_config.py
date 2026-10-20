"""Unit tests for the four new orthogonal execution/sizing-layer dimensions
(build_cfg(overrides=...), no_trade_band, vol_target_mode, dividend_reinvest)
-- these are pure config-layer concerns (validation + defaults), kept
separate from test_state_machine.py since none of them touch
state_machine.py at all (that's the point: they're orthogonal to the state
machine's transition logic, only to the engine's execution/sizing layer).
"""
import pytest

from sgs.config import OVERRIDABLE_PARAMS, build_cfg


def test_overrides_apply_whitelisted_params():
    cfg = build_cfg("BASE", "DEV_EARLY", overrides={"BULL_MA200_RATIO": 1.02})
    assert cfg["BULL_MA200_RATIO"] == 1.02
    assert cfg["OVERRIDES"] == {"BULL_MA200_RATIO": 1.02}


def test_overrides_reject_structural_fields():
    with pytest.raises(ValueError, match="non-overridable"):
        build_cfg("BASE", "DEV_EARLY", overrides={"ROLE_WEIGHTS": {}})
    with pytest.raises(ValueError, match="non-overridable"):
        build_cfg("BASE", "DEV_EARLY", overrides={"ACTUAL_AVAILABLE_FROM": {}})


def test_overrides_every_whitelisted_param_is_individually_settable():
    """Every name in OVERRIDABLE_PARAMS must actually exist in BASE_CFG and
    be settable without error -- catches a typo in the whitelist itself."""
    for param in OVERRIDABLE_PARAMS:
        cfg = build_cfg("BASE", "DEV_EARLY", overrides={param: 999})
        assert cfg[param] == 999


def test_overrides_default_is_empty():
    cfg = build_cfg("BASE", "DEV_EARLY")
    assert cfg["OVERRIDES"] == {}


def test_no_trade_band_rejects_out_of_range_values():
    with pytest.raises(ValueError, match="no_trade_band"):
        build_cfg("BASE", "DEV_EARLY", no_trade_band=-0.01)
    with pytest.raises(ValueError, match="no_trade_band"):
        build_cfg("BASE", "DEV_EARLY", no_trade_band=0.5)


def test_no_trade_band_default_is_backward_compatible():
    cfg = build_cfg("BASE", "DEV_EARLY")
    assert cfg["NO_TRADE_BAND"] == 0.0


def test_vol_target_mode_rejects_unknown_value():
    with pytest.raises(ValueError, match="VOL_TARGET_MODE"):
        build_cfg("BASE", "DEV_EARLY", vol_target_mode="BOGUS")


def test_vol_target_mode_rejects_nonpositive_annual_target():
    with pytest.raises(ValueError, match="vol_target_annual"):
        build_cfg("BASE", "DEV_EARLY", vol_target_mode="VOL_TARGET", vol_target_annual=0.0)
    with pytest.raises(ValueError, match="vol_target_annual"):
        build_cfg("BASE", "DEV_EARLY", vol_target_mode="VOL_TARGET", vol_target_annual=-0.1)


def test_vol_target_mode_default_is_backward_compatible():
    cfg = build_cfg("BASE", "DEV_EARLY")
    assert cfg["VOL_TARGET_MODE"] == "NONE"


def test_dividend_reinvest_rejects_unknown_value():
    with pytest.raises(ValueError, match="DIVIDEND_REINVEST"):
        build_cfg("BASE", "DEV_EARLY", dividend_reinvest="BOGUS")


def test_dividend_reinvest_default_is_backward_compatible():
    cfg = build_cfg("BASE", "DEV_EARLY")
    assert cfg["DIVIDEND_REINVEST"] == "DEFERRED"

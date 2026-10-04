from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from copybot.config import load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    for var in ("IBKR_HOST", "IBKR_PORT", "COPYBOT_DRY_RUN"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "config.yaml"
    shutil.copy(EXAMPLE, path)
    return path


def test_example_config_loads_with_safe_defaults(cfg_path):
    cfg = load_config(cfg_path)
    assert cfg.trading.dry_run is True
    assert cfg.trading.stop_loss_pct == 10.0
    assert cfg.trading.reserve_ratio == 0.25
    assert cfg.trading.extended_hours_buy_fraction == 0.5
    assert cfg.trading.extended_hours_sell_fraction == 1.0
    assert cfg.trading.regt_overnight_check is True
    assert cfg.trading.max_gross_leverage == 4.0
    assert cfg.trading.rebalance_on_signal is True and cfg.trading.rebalance_band_pct == 5.0


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("1", True), ("YES", True), ("false", False), ("0", False)])
def test_dry_run_env_override(cfg_path, monkeypatch, raw, expected):
    monkeypatch.setenv("COPYBOT_DRY_RUN", raw)
    assert load_config(cfg_path).trading.dry_run is expected


def test_dry_run_env_typo_is_rejected(cfg_path, monkeypatch):
    monkeypatch.setenv("COPYBOT_DRY_RUN", "flase")
    with pytest.raises(ValueError):
        load_config(cfg_path)


def test_ibkr_env_overrides(cfg_path, monkeypatch):
    monkeypatch.setenv("IBKR_HOST", "ib-gateway")
    monkeypatch.setenv("IBKR_PORT", "4003")
    cfg = load_config(cfg_path)
    assert (cfg.ibkr.host, cfg.ibkr.port) == ("ib-gateway", 4003)

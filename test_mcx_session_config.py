"""
test_mcx_session_config.py -- Milestone 12, Phase 2C follow-up:
regression tests for mcx_session_config.py (the single source of truth
for MCX's non-agricultural seasonal close times) and app.py's
_mcx_nonagri_close()'s delegation to it.
"""
import datetime as dt
import logging
import os

os.environ["SKIP_AUTOSTART"] = "1"

from unittest.mock import patch

import pytest

import app
import mcx_session_config as mcx_cfg


@pytest.fixture(autouse=True)
def _reset_warned_flag():
    """mcx_session_config._warned_this_process is a module-level
    "log once per process" guard -- reset it before/after every test so
    tests don't leak state into each other (this file's own tests are
    the ONLY thing that would otherwise trip it across the whole suite,
    since app.py only calls warn_if_approximate() under real startup,
    never under SKIP_AUTOSTART=1)."""
    mcx_cfg._warned_this_process = False
    yield
    mcx_cfg._warned_this_process = False


class TestSummerClose:
    def test_default_value(self):
        assert mcx_cfg.MCX_NON_AGRI_SUMMER_CLOSE == "23:55"

    def test_summer_close_parses_to_tuple(self):
        assert mcx_cfg.summer_close() == (23, 55)

    def test_app_uses_it_during_the_dst_window(self):
        # 2026-07-15 -- squarely inside the DST-linked window
        assert app._mcx_nonagri_close(dt.datetime(2026, 7, 15, 12, 0)) == (23, 55)


class TestWinterClose:
    def test_default_value(self):
        assert mcx_cfg.MCX_NON_AGRI_WINTER_CLOSE == "23:30"

    def test_winter_close_parses_to_tuple(self):
        assert mcx_cfg.winter_close() == (23, 30)

    def test_app_uses_it_outside_the_dst_window(self):
        # 2026-12-15 -- outside the DST-linked window
        assert app._mcx_nonagri_close(dt.datetime(2026, 12, 15, 12, 0)) == (23, 30)


class TestWarningEmission:
    def test_warns_when_mode_is_approximate(self, caplog):
        assert mcx_cfg.MCX_DST_MODE == "APPROXIMATE"
        with caplog.at_level(logging.WARNING, logger="oi_dashboard.mcx_session_config"):
            mcx_cfg.warn_if_approximate()
        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == "WARNING"
        assert "not yet" in caplog.records[0].message.lower()
        assert "circular" in caplog.records[0].message.lower()

    def test_does_not_warn_when_mode_is_verified(self, caplog, monkeypatch):
        monkeypatch.setattr(mcx_cfg, "MCX_DST_MODE", "VERIFIED")
        with caplog.at_level(logging.WARNING, logger="oi_dashboard.mcx_session_config"):
            mcx_cfg.warn_if_approximate()
        assert len(caplog.records) == 0

    def test_warns_only_once_per_process(self, caplog):
        with caplog.at_level(logging.WARNING, logger="oi_dashboard.mcx_session_config"):
            mcx_cfg.warn_if_approximate()
            mcx_cfg.warn_if_approximate()
            mcx_cfg.warn_if_approximate()
        assert len(caplog.records) == 1

    def test_second_call_after_reset_warns_again(self, caplog):
        with caplog.at_level(logging.WARNING, logger="oi_dashboard.mcx_session_config"):
            mcx_cfg.warn_if_approximate()
            mcx_cfg._warned_this_process = False   # simulates a fresh process
            mcx_cfg.warn_if_approximate()
        assert len(caplog.records) == 2


class TestConfigOverrideBehavior:
    def test_overriding_summer_close_changes_the_parsed_value(self, monkeypatch):
        monkeypatch.setattr(mcx_cfg, "MCX_NON_AGRI_SUMMER_CLOSE", "22:00")
        assert mcx_cfg.summer_close() == (22, 0)

    def test_overriding_winter_close_changes_the_parsed_value(self, monkeypatch):
        monkeypatch.setattr(mcx_cfg, "MCX_NON_AGRI_WINTER_CLOSE", "21:45")
        assert mcx_cfg.winter_close() == (21, 45)

    def test_app_picks_up_an_overridden_summer_close(self, monkeypatch):
        """Proves app.py's _mcx_nonagri_close() reads mcx_session_config
        at CALL time, not at import time -- a monkeypatched override
        takes effect immediately, with no app.py code change needed."""
        monkeypatch.setattr(mcx_cfg, "MCX_NON_AGRI_SUMMER_CLOSE", "20:00")
        assert app._mcx_nonagri_close(dt.datetime(2026, 7, 15, 12, 0)) == (20, 0)

    def test_app_picks_up_an_overridden_winter_close(self, monkeypatch):
        monkeypatch.setattr(mcx_cfg, "MCX_NON_AGRI_WINTER_CLOSE", "20:30")
        assert app._mcx_nonagri_close(dt.datetime(2026, 12, 15, 12, 0)) == (20, 30)

    def test_is_market_open_reflects_an_overridden_close(self):
        """End-to-end: overriding the config changes is_market_open()'s
        actual boolean result for a real MCX symbol, not just the raw
        tuple _mcx_nonagri_close() returns."""
        gold_cfg = app.SYMBOLS["GOLD"]
        with patch.object(mcx_cfg, "MCX_NON_AGRI_WINTER_CLOSE", "10:00"), \
             patch.object(app, "now_ist", lambda: dt.datetime(2026, 12, 15, 10, 30)):
            open_, reason = app.is_market_open(gold_cfg)
            assert open_ is False
            assert reason == "Outside trading hours"

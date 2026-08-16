"""
test_agents/risk_manager/test_risk_decision.py -- Milestone 25 WS3
regression tests for the one explainable "is a new paper-trade allowed
right now" answer (agents.risk_manager.risk_decision.
evaluate_trade_permission). Uses the paper_db fixture (real-shaped
schema, never app.py), same convention every other risk_manager test
file already follows.
"""
import sqlite3

import pytest

from agents import config
from agents.risk_manager import data_access, risk_decision

from .conftest import insert_engine_trade, insert_paper_order, insert_user


def _insert_ti_trade(db_path, **kwargs):
    """ti_paper_trades' real schema has no entry_ts column (unlike the
    three legacy engine tables) -- a dedicated helper rather than
    reusing conftest.insert_engine_trade, which would try to write one."""
    defaults = {
        "symbol": "NIFTY", "strike": 22000, "direction": "CE", "entry_price": 100.0,
        "target_price": None, "sl_price": None, "qty": 1, "entry_time": "2026-05-04T09:20:00",
        "exit_price": None, "exit_time": None, "exit_reason": None, "points": None, "status": "OPEN",
    }
    defaults.update(kwargs)
    conn = sqlite3.connect(db_path)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" for _ in defaults)
    conn.execute(f"INSERT INTO ti_paper_trades ({cols}) VALUES ({placeholders})", tuple(defaults.values()))
    conn.commit()
    conn.close()


@pytest.fixture()
def small_limits(monkeypatch):
    """Small, easy-to-cross thresholds so tests don't need six-figure
    fixture data to exercise a limit."""
    monkeypatch.setattr(config, "RISK_ACCOUNT_CAPITAL", 100_000.0)
    monkeypatch.setattr(config, "RISK_DAILY_LOSS_LIMIT_PCT", 3.0)      # limit = 3,000
    monkeypatch.setattr(config, "RISK_MAX_EXPOSURE_PER_SYMBOL_PCT", 8.0)   # limit = 8,000
    monkeypatch.setattr(config, "RISK_PORTFOLIO_HEAT_LIMIT_PCT", 6.0)      # limit = 6,000
    monkeypatch.setattr(config, "RISK_MAX_CONCURRENT_STRATEGIES", 3)
    monkeypatch.setattr(config, "PAPER_TRADE_LOT_QTY", 1)
    monkeypatch.setattr(risk_decision, "WARNING_THRESHOLD_PCT", 70.0)


class TestNormalAndWarningStates:
    def test_no_positions_no_losses_is_normal_and_allowed(self, paper_db, small_limits):
        result = risk_decision.evaluate_trade_permission()
        assert result["allowed"] is True
        assert result["risk_state"] == risk_decision.RISK_STATE_NORMAL
        assert result["daily_loss"] == 0.0
        assert result["current_exposure"] == 0.0

    def test_daily_loss_below_warning_threshold_is_normal(self, paper_db, small_limits, frozen_today):
        # loss = 2000, limit = 3000 -> 66.7% of limit, below the 70% warning band
        insert_paper_order(paper_db, user_id=1, entry_price=100.0, qty=1, status="CLOSED",
                            exit_price=None, points=-2000.0, exit_time="2026-08-15T10:00:00")
        result = risk_decision.evaluate_trade_permission()
        assert result["risk_state"] == risk_decision.RISK_STATE_NORMAL
        assert result["allowed"] is True
        assert result["daily_loss"] == 2000.0

    def test_daily_loss_crossing_warning_threshold_is_warning_but_still_allowed(self, paper_db, small_limits, frozen_today):
        # loss = 2500, limit = 3000 -> 83.3% of limit, above the 70% warning band, below 100%
        insert_paper_order(paper_db, user_id=1, entry_price=100.0, qty=1, status="CLOSED",
                            points=-2500.0, exit_time="2026-08-15T10:00:00")
        result = risk_decision.evaluate_trade_permission()
        assert result["risk_state"] == risk_decision.RISK_STATE_WARNING
        assert result["allowed"] is True


class TestDailyLossLimit:
    def test_daily_loss_at_limit_is_blocked(self, paper_db, small_limits, frozen_today):
        insert_paper_order(paper_db, user_id=1, entry_price=100.0, qty=1, status="CLOSED",
                            points=-3000.0, exit_time="2026-08-15T10:00:00")
        result = risk_decision.evaluate_trade_permission()
        assert result["risk_state"] == risk_decision.RISK_STATE_LIMIT_REACHED
        assert result["allowed"] is False
        assert "daily loss limit" in result["reason"]

    def test_daily_loss_exceeding_limit_is_blocked(self, paper_db, small_limits, frozen_today):
        insert_paper_order(paper_db, user_id=1, entry_price=100.0, qty=1, status="CLOSED",
                            points=-5000.0, exit_time="2026-08-15T10:00:00")
        result = risk_decision.evaluate_trade_permission()
        assert result["allowed"] is False
        assert result["daily_loss"] == 5000.0

    def test_profitable_day_never_blocks_on_daily_loss(self, paper_db, small_limits):
        insert_paper_order(paper_db, user_id=1, entry_price=100.0, qty=1, status="CLOSED",
                            points=9000.0, exit_time="2026-08-15T10:00:00")
        result = risk_decision.evaluate_trade_permission()
        assert result["daily_loss"] == 0.0   # a gain is never a "loss"
        assert result["allowed"] is True

    def test_losses_closed_before_todays_boundary_are_excluded(self, paper_db, small_limits, monkeypatch):
        """Milestone 25 WS3: the daily-loss boundary is IST calendar-day,
        via agents.timekeeping.now_ist() -- not the OS's own local date.
        A loss closed "yesterday" (by IST) must not count against today's
        limit."""
        import datetime as dt

        from agents import timekeeping

        monkeypatch.setattr(timekeeping, "now_ist", lambda: dt.datetime(2026, 8, 15, 0, 5, 0))
        insert_paper_order(paper_db, user_id=1, entry_price=100.0, qty=1, status="CLOSED",
                            points=-5000.0, exit_time="2026-08-14T23:59:00")
        result = risk_decision.evaluate_trade_permission()
        assert result["daily_loss"] == 0.0
        assert result["allowed"] is True


class TestExposureLimit:
    def test_exposure_below_limit_is_normal(self, paper_db, small_limits):
        insert_paper_order(paper_db, user_id=1, entry_price=50.0, qty=1, status="OPEN")
        result = risk_decision.evaluate_trade_permission(symbol="NIFTY")
        assert result["current_exposure"] == 50.0
        assert result["risk_state"] == risk_decision.RISK_STATE_NORMAL
        assert result["allowed"] is True

    def test_exposure_at_symbol_limit_is_blocked(self, paper_db, small_limits):
        insert_paper_order(paper_db, user_id=1, symbol="NIFTY", entry_price=8000.0, qty=1, status="OPEN")
        result = risk_decision.evaluate_trade_permission(symbol="NIFTY")
        assert result["current_exposure"] == 8000.0
        assert result["exposure_limit"] == 8000.0
        assert result["risk_state"] == risk_decision.RISK_STATE_LIMIT_REACHED
        assert result["allowed"] is False

    def test_exposure_exceeding_symbol_limit_is_blocked(self, paper_db, small_limits):
        insert_paper_order(paper_db, user_id=1, symbol="NIFTY", entry_price=12000.0, qty=1, status="OPEN")
        result = risk_decision.evaluate_trade_permission(symbol="NIFTY")
        assert result["allowed"] is False

    def test_no_symbol_given_checks_portfolio_wide_heat_limit_instead(self, paper_db, small_limits):
        # Single-symbol exposure (7000) is under the per-symbol limit (8000)
        # but at the tighter, portfolio-wide heat limit (6000) -- proves
        # `symbol=None` uses RISK_PORTFOLIO_HEAT_LIMIT_PCT, not
        # RISK_MAX_EXPOSURE_PER_SYMBOL_PCT.
        insert_paper_order(paper_db, user_id=1, symbol="NIFTY", entry_price=7000.0, qty=1, status="OPEN")
        scoped = risk_decision.evaluate_trade_permission(symbol="NIFTY")
        portfolio_wide = risk_decision.evaluate_trade_permission(symbol=None)
        assert scoped["exposure_limit"] == 8000.0
        assert portfolio_wide["exposure_limit"] == 6000.0
        assert scoped["allowed"] is True
        assert portfolio_wide["allowed"] is False


class TestMultiplePositionsAndSymbols:
    def test_multiple_open_positions_sum_exposure_across_engines(self, paper_db, small_limits):
        insert_paper_order(paper_db, user_id=1, symbol="NIFTY", entry_price=1000.0, qty=1, status="OPEN")
        insert_engine_trade(paper_db, "paper_trades", symbol="NIFTY", entry_price=500.0, status="OPEN")
        insert_engine_trade(paper_db, "scalp_paper_trades", symbol="NIFTY", entry_price=500.0, status="OPEN")
        result = risk_decision.evaluate_trade_permission(symbol="NIFTY")
        assert result["current_exposure"] == 2000.0
        assert result["open_position_count"] == 3

    def test_multiple_symbols_scoped_exposure_only_counts_the_given_symbol(self, paper_db, small_limits):
        insert_paper_order(paper_db, user_id=1, symbol="NIFTY", entry_price=7900.0, qty=1, status="OPEN")
        insert_paper_order(paper_db, user_id=1, symbol="BANKNIFTY", entry_price=7900.0, qty=1, status="OPEN")
        nifty = risk_decision.evaluate_trade_permission(symbol="NIFTY")
        banknifty = risk_decision.evaluate_trade_permission(symbol="BANKNIFTY")
        assert nifty["current_exposure"] == 7900.0   # not 15800 -- the other symbol's position is excluded
        assert banknifty["current_exposure"] == 7900.0
        # But total open_position_count is portfolio-wide, not symbol-scoped.
        assert nifty["open_position_count"] == 2

    def test_concurrent_strategies_limit_blocks_regardless_of_pnl_or_exposure(self, paper_db, small_limits):
        for i in range(3):
            insert_paper_order(paper_db, user_id=1, symbol=f"SYM{i}", entry_price=1.0, qty=1, status="OPEN")
        result = risk_decision.evaluate_trade_permission(symbol="NIFTY")
        assert result["risk_state"] == risk_decision.RISK_STATE_BLOCKED
        assert result["allowed"] is False
        assert "concurrent limit" in result["reason"]


class TestMissingQuantityInformation:
    def test_legacy_engine_positions_flag_estimated_qty_honestly(self, paper_db, small_limits):
        insert_engine_trade(paper_db, "paper_trades", symbol="NIFTY", entry_price=50.0, status="OPEN")
        result = risk_decision.evaluate_trade_permission()
        assert result["qty_basis_note"] is not None
        assert "no persisted per-trade quantity" in result["qty_basis_note"]

    def test_ti_and_paper_orders_positions_never_trigger_the_estimate_note(self, paper_db, small_limits):
        insert_paper_order(paper_db, user_id=1, symbol="NIFTY", entry_price=50.0, qty=10, status="OPEN")
        _insert_ti_trade(paper_db, symbol="BANKNIFTY", entry_price=50.0, qty=15, status="OPEN")
        result = risk_decision.evaluate_trade_permission()
        assert result["qty_basis_note"] is None


class TestTiPaperTradesUnitsCorrectness:
    """Milestone 25 WS3's own core correctness fix: ti_paper_trades.points
    is already qty-scaled (rupee terms); the three legacy engine tables'
    `points` are raw, unscaled premium differences. These must combine
    correctly, not get summed as if they were the same unit."""

    def test_ti_loss_is_not_double_or_under_counted_against_legacy_points(self, paper_db, small_limits, frozen_today):
        # TI: (95 - 100) * qty=20 = -100 rupees, already scaled -- stored as-is.
        _insert_ti_trade(paper_db, symbol="NIFTY", entry_price=100.0, exit_price=95.0, qty=20,
                          points=-100.0, status="CLOSED", exit_time="2026-08-15T10:00:00")
        # Legacy (paper_orders): raw points -10, * PAPER_TRADE_LOT_QTY=1 -> -10 rupees.
        insert_paper_order(paper_db, user_id=1, entry_price=100.0, qty=1, status="CLOSED",
                            points=-10.0, exit_time="2026-08-15T10:05:00")
        result = risk_decision.evaluate_trade_permission()
        assert result["daily_loss"] == 110.0   # 100 + 10, not 100*1 + 10*20 or any other cross-scaled figure

    def test_ti_loss_alone_is_used_as_is_not_multiplied_by_lot_qty_again(self, paper_db, small_limits, frozen_today, monkeypatch):
        monkeypatch.setattr(config, "PAPER_TRADE_LOT_QTY", 5)   # would corrupt the result if wrongly applied to TI
        _insert_ti_trade(paper_db, symbol="NIFTY", entry_price=100.0, exit_price=90.0, qty=20,
                          points=-200.0, status="CLOSED", exit_time="2026-08-15T10:00:00")
        result = risk_decision.evaluate_trade_permission()
        assert result["daily_loss"] == 200.0   # NOT 200 * 5 = 1000


class TestRestartRecovery:
    def test_result_is_recomputed_purely_from_persisted_state_no_in_process_memory(self, paper_db, small_limits, frozen_today):
        """risk_decision holds no module-level state of its own -- every
        field is recomputed fresh from the DB on each call, so a process
        restart changes nothing about correctness. Simulated here by
        calling evaluate_trade_permission() repeatedly with DB writes in
        between, exactly as a fresh process after restart would see
        whatever was last persisted."""
        first = risk_decision.evaluate_trade_permission(symbol="NIFTY")
        assert first["current_exposure"] == 0.0

        insert_paper_order(paper_db, user_id=1, symbol="NIFTY", entry_price=500.0, qty=1, status="OPEN")
        second = risk_decision.evaluate_trade_permission(symbol="NIFTY")
        assert second["current_exposure"] == 500.0

        insert_paper_order(paper_db, user_id=1, symbol="NIFTY", entry_price=100.0, qty=1, status="CLOSED",
                            points=-3000.0, exit_time="2026-08-15T10:00:00")
        third = risk_decision.evaluate_trade_permission(symbol="NIFTY")
        assert third["allowed"] is False
        assert third["risk_state"] == risk_decision.RISK_STATE_LIMIT_REACHED


class TestResultShape:
    def test_contains_every_documented_field(self, paper_db, small_limits):
        result = risk_decision.evaluate_trade_permission(symbol="NIFTY")
        for key in (
            "allowed", "risk_state", "reason", "daily_loss", "daily_loss_limit", "current_exposure",
            "exposure_limit", "remaining_capacity", "open_position_count", "symbol", "qty_basis_note",
            "timestamp",
        ):
            assert key in result
        assert result["symbol"] == "NIFTY"
        assert result["risk_state"] in (
            risk_decision.RISK_STATE_NORMAL, risk_decision.RISK_STATE_WARNING,
            risk_decision.RISK_STATE_LIMIT_REACHED, risk_decision.RISK_STATE_BLOCKED,
        )

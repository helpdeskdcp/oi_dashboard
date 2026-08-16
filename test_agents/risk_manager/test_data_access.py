"""
test_agents/risk_manager/test_data_access.py -- regression tests for
agents/risk_manager/data_access.py against the minimal real-shaped
schema in this package's own conftest.py (paper_db fixture) -- never a
real oi_history.db, never app.py.
"""
import sqlite3

from agents.risk_manager import data_access
from .conftest import insert_engine_trade, insert_paper_order, insert_strikes, insert_user


def _insert_ti_trade(db_path, **kwargs):
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


class TestOpenPositionsForUser:
    def test_only_returns_that_users_open_orders(self, paper_db):
        insert_user(paper_db, 1)
        insert_paper_order(paper_db, user_id=1, status="OPEN")
        insert_paper_order(paper_db, user_id=2, status="OPEN")
        insert_paper_order(paper_db, user_id=1, status="CLOSED")
        positions = data_access.open_positions_for_user(1)
        assert len(positions) == 1
        assert positions[0].user_id == 1
        assert positions[0].source == "paper_orders"


class TestOpenSystemPositions:
    def test_unions_all_three_engine_tables(self, paper_db):
        insert_engine_trade(paper_db, "paper_trades", status="OPEN")
        insert_engine_trade(paper_db, "scalp_paper_trades", status="OPEN")
        insert_engine_trade(paper_db, "v3_paper_trades", status="CLOSED")
        positions = data_access.open_system_positions()
        assert len(positions) == 2
        assert {p.source for p in positions} == {"paper_trades", "scalp_paper_trades"}

    def test_milestone25_also_includes_ti_paper_trades(self, paper_db):
        """The M25 audit's own headline data_access.py finding: the Live
        Portfolio Risk Monitor previously had zero visibility into the
        Trading Intelligence engine's positions at all."""
        insert_engine_trade(paper_db, "paper_trades", status="OPEN")
        _insert_ti_trade(paper_db, status="OPEN")
        _insert_ti_trade(paper_db, status="CLOSED")
        positions = data_access.open_system_positions()
        assert len(positions) == 2
        assert {p.source for p in positions} == {"paper_trades", "ti_paper_trades"}

    def test_qty_is_estimated_flag_distinguishes_real_from_fallback_quantity(self, paper_db):
        insert_engine_trade(paper_db, "paper_trades", status="OPEN")
        _insert_ti_trade(paper_db, qty=25, status="OPEN")
        positions = {p.source: p for p in data_access.open_system_positions()}
        assert positions["paper_trades"].qty_is_estimated is True
        assert positions["paper_trades"].qty == 1   # no real qty column -- fallback default
        assert positions["ti_paper_trades"].qty_is_estimated is False
        assert positions["ti_paper_trades"].qty == 25   # real, persisted quantity

    def test_paper_orders_qty_is_never_flagged_as_estimated(self, paper_db):
        insert_user(paper_db, 1)
        insert_paper_order(paper_db, user_id=1, qty=10, status="OPEN")
        assert data_access.open_positions_for_user(1)[0].qty_is_estimated is False


class TestAllOpenPositions:
    def test_user_id_includes_system_positions_too(self, paper_db):
        insert_user(paper_db, 1)
        insert_paper_order(paper_db, user_id=1, status="OPEN")
        insert_engine_trade(paper_db, "paper_trades", status="OPEN")
        positions = data_access.all_open_positions(user_id=1)
        assert len(positions) == 2

    def test_none_returns_every_users_orders_plus_system_positions(self, paper_db):
        insert_user(paper_db, 1)
        insert_user(paper_db, 2)
        insert_paper_order(paper_db, user_id=1, status="OPEN")
        insert_paper_order(paper_db, user_id=2, status="OPEN")
        insert_engine_trade(paper_db, "v3_paper_trades", status="OPEN")
        positions = data_access.all_open_positions()
        assert len(positions) == 3


class TestWalletBalance:
    def test_returns_the_users_balance(self, paper_db):
        insert_user(paper_db, 1, wallet_balance=12345.0)
        assert data_access.wallet_balance(1) == 12345.0

    def test_unknown_user_returns_zero(self, paper_db):
        assert data_access.wallet_balance(999) == 0.0


class TestDailyRealizedPnl:
    def test_sums_closed_positions_across_all_tables(self, paper_db):
        insert_paper_order(paper_db, status="CLOSED", exit_time="2026-05-04T10:00:00", points=100.0)
        insert_engine_trade(paper_db, "paper_trades", status="CLOSED", exit_time="2026-05-04T11:00:00", points=50.0)
        insert_engine_trade(
            paper_db, "scalp_paper_trades", status="CLOSED", exit_time="2026-05-03T11:00:00", points=999.0,
        )  # before since_date -- excluded
        total = data_access.daily_realized_pnl(None, since_date="2026-05-04")
        assert total == 150.0

    def test_filters_by_user_for_paper_orders(self, paper_db):
        insert_paper_order(paper_db, user_id=1, status="CLOSED", exit_time="2026-05-04T10:00:00", points=100.0)
        insert_paper_order(paper_db, user_id=2, status="CLOSED", exit_time="2026-05-04T10:00:00", points=999.0)
        assert data_access.daily_realized_pnl(1, since_date="2026-05-04") == 100.0

    def test_ti_paper_trades_are_deliberately_excluded_here(self, paper_db):
        """Milestone 25 WS3: ti_paper_trades.points is already quantity-
        scaled (see this module's own UNITS WARNING) -- folding it into
        this SUM would silently mix units with the four raw-points
        tables. Use ti_daily_realized_pnl() for TI's own figure."""
        insert_paper_order(paper_db, status="CLOSED", exit_time="2026-05-04T10:00:00", points=100.0)
        _insert_ti_trade(paper_db, status="CLOSED", exit_time="2026-05-04T10:00:00", points=99999.0, qty=100)
        assert data_access.daily_realized_pnl(None, since_date="2026-05-04") == 100.0


class TestTiDailyRealizedPnl:
    def test_sums_closed_ti_trades_since_date(self, paper_db):
        _insert_ti_trade(paper_db, status="CLOSED", exit_time="2026-05-04T10:00:00", points=-250.0, qty=50)
        _insert_ti_trade(paper_db, status="CLOSED", exit_time="2026-05-04T11:00:00", points=100.0, qty=20)
        _insert_ti_trade(paper_db, status="CLOSED", exit_time="2026-05-03T11:00:00", points=999.0, qty=1)  # excluded
        _insert_ti_trade(paper_db, status="OPEN")   # excluded -- not closed
        assert data_access.ti_daily_realized_pnl(since_date="2026-05-04") == -150.0

    def test_zero_when_nothing_closed(self, paper_db):
        assert data_access.ti_daily_realized_pnl(since_date="2026-05-04") == 0.0

    def test_never_reads_legacy_engine_tables(self, paper_db):
        insert_paper_order(paper_db, status="CLOSED", exit_time="2026-05-04T10:00:00", points=99999.0)
        assert data_access.ti_daily_realized_pnl(since_date="2026-05-04") == 0.0


class TestClosedTradePointsToday:
    def test_returns_ordered_points(self, paper_db):
        insert_paper_order(paper_db, status="CLOSED", exit_time="2026-05-04T11:00:00", points=-10.0)
        insert_paper_order(paper_db, status="CLOSED", exit_time="2026-05-04T09:00:00", points=20.0)
        points = data_access.closed_trade_points_today(None, since_date="2026-05-04")
        assert points == [20.0, -10.0]

    def test_ti_paper_trades_excluded_here_too_same_units_reasoning(self, paper_db):
        """Milestone 25 WS3: ti_paper_trades.points is quantity-scaled,
        the four tables this function reads are not -- summing/ordering
        them together would silently mix units, same reasoning as
        daily_realized_pnl() below."""
        insert_paper_order(paper_db, status="CLOSED", exit_time="2026-05-04T11:00:00", points=-10.0)
        _insert_ti_trade(paper_db, status="CLOSED", exit_time="2026-05-04T10:00:00", points=-5000.0, qty=50)
        points = data_access.closed_trade_points_today(None, since_date="2026-05-04")
        assert points == [-10.0]


class TestLatestGreeksForStrike:
    def test_returns_the_most_recent_row(self, paper_db):
        insert_strikes(paper_db, "NIFTY", 22000, ts="2026-05-04T09:00:00", ce_delta=0.4)
        insert_strikes(paper_db, "NIFTY", 22000, ts="2026-05-04T10:00:00", ce_delta=0.5)
        greeks = data_access.latest_greeks_for_strike("NIFTY", 22000, "CE")
        assert greeks["delta"] == 0.5

    def test_none_when_nothing_logged(self, paper_db):
        assert data_access.latest_greeks_for_strike("NIFTY", 99999, "CE") is None

    def test_reads_put_side_columns_for_pe(self, paper_db):
        insert_strikes(paper_db, "NIFTY", 22000, pe_delta=-0.4, pe_theta=-1.2)
        greeks = data_access.latest_greeks_for_strike("NIFTY", 22000, "PE")
        assert greeks["delta"] == -0.4
        assert greeks["theta"] == -1.2

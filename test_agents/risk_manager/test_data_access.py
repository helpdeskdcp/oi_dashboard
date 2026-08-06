"""
test_agents/risk_manager/test_data_access.py -- regression tests for
agents/risk_manager/data_access.py against the minimal real-shaped
schema in this package's own conftest.py (paper_db fixture) -- never a
real oi_history.db, never app.py.
"""
from agents.risk_manager import data_access
from .conftest import insert_engine_trade, insert_paper_order, insert_strikes, insert_user


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


class TestClosedTradePointsToday:
    def test_returns_ordered_points(self, paper_db):
        insert_paper_order(paper_db, status="CLOSED", exit_time="2026-05-04T11:00:00", points=-10.0)
        insert_paper_order(paper_db, status="CLOSED", exit_time="2026-05-04T09:00:00", points=20.0)
        points = data_access.closed_trade_points_today(None, since_date="2026-05-04")
        assert points == [20.0, -10.0]


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

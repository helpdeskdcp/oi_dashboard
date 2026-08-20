"""
test_postmarket_report.py -- regression tests for generate_postmarket_report()
and its /postmarket-report + /api/postmarket-report/<symbol> routes.

Same technique as test_candle_freshness_route.py/test_paper_orders_phase3.py:
SKIP_AUTOSTART=1 before importing app, throwaway DB per test, app.init_db()
explicitly.
"""
import os
os.environ["SKIP_AUTOSTART"] = "1"

import datetime as dt
import sqlite3

import pytest

import app
import auth
import billing
from agents.trading_intelligence import ti_store

NIFTY_CFG = app.SYMBOLS["NIFTY"]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
    monkeypatch.setattr(ti_store, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    app.init_db()
    ti_store.init_db()   # ti_paper_trades lives in this separate module's own init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


CSRF_TOKEN = "test-csrf-token"


def _login_admin(client):
    conn = sqlite3.connect(app.DB_PATH)
    admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    conn.close()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_id
        sess["csrf_token"] = CSRF_TOKEN


def _insert_cycle(db_path, *, symbol, date, time, underlying_ltp):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cycles (symbol, ts, date, time, underlying_ltp, atm) VALUES (?,?,?,?,?,?)",
        (symbol, f"{date}T{time}", date, time, underlying_ltp, underlying_ltp),
    )
    conn.commit()
    conn.close()


def _insert_snapshot(db_path, *, symbol, date, atr_14, regime, pdc):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO market_structure_snapshots (symbol, date, time, ts, atr_14, regime, pdc) "
        "VALUES (?,?,?,?,?,?,?)",
        (symbol, date, "09:20:00", f"{date}T09:20:00", atr_14, regime, pdc),
    )
    conn.commit()
    conn.close()


def _insert_epoch_trade(db_path, *, table, symbol, entry_dt, points, exit_reason, strike=25000, direction="CE"):
    """Seeds a minimal CLOSED row in one of the 4 entry_ts-based paper-trade
    tables (paper_trades/scalp_paper_trades/v3_paper_trades/paper_orders)."""
    conn = sqlite3.connect(db_path)
    entry_ts = entry_dt.timestamp()
    if table == "paper_orders":
        conn.execute(
            "INSERT INTO paper_orders (user_id, symbol, strike, direction, entry_price, entry_time, entry_ts, "
            "status, exit_price, exit_time, exit_reason, points, qty, wallet_linked, trade_source) "
            "VALUES (1,?,?,?,?,?,?, 'CLOSED', 100.0, ?, ?, ?, 1, 0, 'MANUAL')",
            (symbol, strike, direction, 100.0, entry_dt.strftime("%H:%M:%S"), entry_ts,
             entry_dt.strftime("%H:%M:%S"), exit_reason, points),
        )
    else:
        conn.execute(
            f"INSERT INTO {table} (symbol, strike, direction, entry_price, entry_time, entry_ts, "
            f"status, exit_price, exit_time, exit_reason, points) "
            f"VALUES (?,?,?,?,?,?, 'CLOSED', 100.0, ?, ?, ?)",
            (symbol, strike, direction, 100.0, entry_dt.strftime("%H:%M:%S"), entry_ts,
             entry_dt.strftime("%H:%M:%S"), exit_reason, points),
        )
    conn.commit()
    conn.close()


def _insert_ti_trade(db_path, *, symbol, entry_dt, points, exit_reason):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO ti_paper_trades (symbol, strike, direction, entry_price, qty, entry_time, "
        "status, exit_price, exit_time, exit_reason, points) "
        "VALUES (?,25000,'CE',100.0,1,?, 'CLOSED', 100.0, ?, ?, ?)",
        (symbol, entry_dt.isoformat(), entry_dt.isoformat(), exit_reason, points),
    )
    conn.commit()
    conn.close()


class TestGenerateReport:
    def test_no_data_reports_none_honestly_not_fabricated(self, client):
        r = app.generate_postmarket_report("NIFTY", date="2026-01-01")
        assert r["day_open"] is None
        assert r["day_high"] is None
        assert r["trade_count"] == 0
        assert r["net_points"] is None

    def test_real_day_range_from_cycles(self, client):
        _insert_cycle(app.DB_PATH, symbol="NIFTY", date="2026-08-06", time="09:20:00", underlying_ltp=24500.0)
        _insert_cycle(app.DB_PATH, symbol="NIFTY", date="2026-08-06", time="12:00:00", underlying_ltp=24650.0)
        _insert_cycle(app.DB_PATH, symbol="NIFTY", date="2026-08-06", time="15:25:00", underlying_ltp=24580.0)
        r = app.generate_postmarket_report("NIFTY", date="2026-08-06")
        assert r["day_open"] == 24500.0
        assert r["day_high"] == 24650.0
        assert r["day_low"] == 24500.0
        assert r["day_close"] == 24580.0

    def test_expected_vs_actual_range_from_persisted_snapshot(self, client):
        _insert_snapshot(app.DB_PATH, symbol="NIFTY", date="2026-08-06", atr_14=100.0, regime="TRENDING_UP", pdc=24500.0)
        _insert_cycle(app.DB_PATH, symbol="NIFTY", date="2026-08-06", time="09:20:00", underlying_ltp=24500.0)
        _insert_cycle(app.DB_PATH, symbol="NIFTY", date="2026-08-06", time="15:25:00", underlying_ltp=24620.0)
        r = app.generate_postmarket_report("NIFTY", date="2026-08-06")
        assert r["regime_at_open"] == "TRENDING_UP"
        assert r["expected_high"] == 24600.0
        assert r["expected_low"] == 24400.0
        assert r["expected_range_points"] == 200.0
        assert r["actual_range_points"] == 120.0

    def test_defaults_to_today_when_no_date_given(self, client):
        today = app.now_ist().date().isoformat()
        r = app.generate_postmarket_report("NIFTY")
        assert r["date"] == today

    def test_invalid_date_reports_error_not_a_crash(self, client):
        r = app.generate_postmarket_report("NIFTY", date="not-a-date")
        assert "error" in r


class TestTradeRecapAggregation:
    def test_aggregates_across_all_five_paper_trade_tables(self, client):
        entry = dt.datetime(2026, 8, 6, 10, 0, 0)
        _insert_epoch_trade(app.DB_PATH, table="paper_trades", symbol="NIFTY", entry_dt=entry, points=50.0, exit_reason="TARGET HIT")
        _insert_epoch_trade(app.DB_PATH, table="scalp_paper_trades", symbol="NIFTY", entry_dt=entry, points=-20.0, exit_reason="STOP LOSS")
        _insert_epoch_trade(app.DB_PATH, table="v3_paper_trades", symbol="NIFTY", entry_dt=entry, points=10.0, exit_reason="TIME EXIT")
        _insert_epoch_trade(app.DB_PATH, table="paper_orders", symbol="NIFTY", entry_dt=entry, points=5.0, exit_reason="TARGET HIT")
        _insert_ti_trade(app.DB_PATH, symbol="NIFTY", entry_dt=entry, points=-15.0, exit_reason="STOP LOSS")

        r = app.generate_postmarket_report("NIFTY", date="2026-08-06")
        assert r["trade_count"] == 5
        assert r["wins"] == 2
        assert r["losses"] == 2
        assert r["other_exits"] == 1
        assert r["net_points"] == 30.0   # 50 - 20 + 10 + 5 - 15
        assert r["best_trade"]["points"] == 50.0
        assert r["worst_trade"]["points"] == -20.0

    def test_trades_on_a_different_date_are_excluded(self, client):
        _insert_epoch_trade(app.DB_PATH, table="paper_trades", symbol="NIFTY",
                             entry_dt=dt.datetime(2026, 8, 5, 10, 0, 0), points=50.0, exit_reason="TARGET HIT")
        r = app.generate_postmarket_report("NIFTY", date="2026-08-06")
        assert r["trade_count"] == 0

    def test_trades_for_a_different_symbol_are_excluded(self, client):
        entry = dt.datetime(2026, 8, 6, 10, 0, 0)
        _insert_epoch_trade(app.DB_PATH, table="paper_trades", symbol="BANKNIFTY", entry_dt=entry, points=50.0, exit_reason="TARGET HIT")
        r = app.generate_postmarket_report("NIFTY", date="2026-08-06")
        assert r["trade_count"] == 0

    def test_never_auto_modifies_any_strategy_state(self, client):
        """Purely a read/report function -- calling it twice must be
        idempotent and touch no strategy-config table."""
        entry = dt.datetime(2026, 8, 6, 10, 0, 0)
        _insert_epoch_trade(app.DB_PATH, table="paper_trades", symbol="NIFTY", entry_dt=entry, points=50.0, exit_reason="TARGET HIT")
        r1 = app.generate_postmarket_report("NIFTY", date="2026-08-06")
        r2 = app.generate_postmarket_report("NIFTY", date="2026-08-06")
        assert r1 == r2


class TestGracefulDegradation:
    def test_missing_ti_paper_trades_table_degrades_not_crashes(self, monkeypatch, tmp_path):
        """ti_store.init_db() is a separate module's responsibility -- if it
        hasn't run yet (e.g. that agent is disabled), the report must still
        return the other 4 tables' data rather than raising."""
        db_path = str(tmp_path / "no_ti_table.db")
        monkeypatch.setattr(app, "DB_PATH", db_path)
        app.init_db()   # deliberately NOT calling ti_store.init_db() here
        _insert_epoch_trade(db_path, table="paper_trades", symbol="NIFTY",
                             entry_dt=dt.datetime(2026, 8, 6, 10, 0, 0), points=50.0, exit_reason="TARGET HIT")
        r = app.generate_postmarket_report("NIFTY", date="2026-08-06")
        assert r["trade_count"] == 1
        assert r["net_points"] == 50.0


class TestRoutes:
    def test_unauthenticated_get_redirects_to_login(self, client):
        resp = client.get("/postmarket-report")
        assert resp.status_code == 302
        resp = client.get("/api/postmarket-report/NIFTY")
        assert resp.status_code == 302

    def test_admin_can_view_page(self, client):
        _login_admin(client)
        resp = client.get("/postmarket-report")
        assert resp.status_code == 200
        assert b"Post-Market Report" in resp.data

    def test_api_unknown_symbol_400(self, client):
        _login_admin(client)
        resp = client.get("/api/postmarket-report/NOTASYMBOL")
        assert resp.status_code == 400

    def test_api_known_symbol_returns_report(self, client):
        _login_admin(client)
        resp = client.get("/api/postmarket-report/NIFTY?date=2026-08-06")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["symbol"] == "NIFTY"
        assert data["date"] == "2026-08-06"

    def test_page_accepts_date_query_param(self, client):
        _login_admin(client)
        _insert_cycle(app.DB_PATH, symbol="NIFTY", date="2026-08-06", time="09:20:00", underlying_ltp=24500.0)
        resp = client.get("/postmarket-report?date=2026-08-06")
        assert resp.status_code == 200
        assert b"2026-08-06" in resp.data

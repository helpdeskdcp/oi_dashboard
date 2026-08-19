"""
test_legacy_paper_trades_expiry.py -- regression coverage for the
expiry-contract-identity bug in the three legacy, single-open-trade-per-symbol
paper-trading engines: update_paper_trading (paper_trades / S-R Engine),
update_scalp_paper_trading (scalp_paper_trades), and update_v3_paper_trading
(v3_paper_trades).

Same bug class already fixed for ti_paper_trades (PR #30) and paper_orders
(PR #32): matching an open trade's strike against `rows` with no check that
`rows` still reflects the SAME option contract (same expiry) lets a rolled
contract's fresh price silently masquerade as the old one's. Real, live-stuck
evidence found before this fix: paper_trades id 311 (GOLD 155000 CE, entered
2026-08-19 18:05, strike vanished from the chain at 18:06:39 -- the TIME EXIT
ceiling never fired because current_price stayed None forever once the strike
stopped matching any row).

Unlike paper_orders (many concurrent DB rows per symbol), these three each
track exactly ONE open_trade per symbol, held in an in-memory bucket
(app.state["paper_by_symbol"/"scalp_paper_by_symbol"/"v3_paper_by_symbol"])
and mirrored to its own DB table. Same technique as test_paper_orders_phase3.py:
SKIP_AUTOSTART=1 before importing app, throwaway DB per test, app.init_db()
explicitly, and a monkeypatched data_access.DB_PATH so recent_strike_history()
reads the same throwaway DB instead of production.
"""
import os
os.environ["SKIP_AUTOSTART"] = "1"

import sqlite3
import datetime as dt
from unittest.mock import patch

import pytest

import app
import auth
import billing
from agents.trading_intelligence import data_access
from oi_engine import StrikeRow

NIFTY_CFG = app.SYMBOLS["NIFTY"]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
    monkeypatch.setattr(data_access, "DB_PATH", db_path)
    app.init_db()
    app.state["paper_by_symbol"].clear()
    app.state["scalp_paper_by_symbol"].clear()
    app.state["v3_paper_by_symbol"].clear()
    app.state.get("sr_active_profile_cache", {}).clear()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _insert_cycle_and_strike(db_path, *, symbol, ts, strike, ce_ltp=None, pe_ltp=None):
    """Minimal real cycles/strikes rows so recent_strike_history() has genuine
    pre-rollover history to find -- same tables app.py's own log_cycle_to_db()
    writes in production."""
    conn = sqlite3.connect(db_path)
    date, time = ts.split("T")
    cur = conn.execute(
        "INSERT INTO cycles (symbol, ts, date, time, underlying_ltp, atm) VALUES (?,?,?,?,?,?)",
        (symbol, ts, date, time, strike, strike),
    )
    cycle_id = cur.lastrowid
    conn.execute(
        "INSERT INTO strikes (cycle_id, strike, ce_ltp, pe_ltp) VALUES (?,?,?,?)",
        (cycle_id, strike, ce_ltp, pe_ltp),
    )
    conn.commit()
    conn.close()


def _open_sr_trade(symbol="NIFTY", strike=25000, direction="CE", entry_price=100.0,
                    target_price=160.0, sl_price=80.0, expiry_date_at_entry="2026-08-06"):
    bucket = app.paper_trade_bucket(symbol)
    bucket["open_trade"] = {
        "symbol": symbol, "strike": strike, "direction": direction,
        "entry_price": entry_price, "target_price": target_price, "sl_price": sl_price,
        "entry_time": "09:20:00", "entry_time_obj": app.now_ist(),
        "confidence": 70, "current_price": entry_price, "points_now": 0.0,
        "expiry_date_at_entry": expiry_date_at_entry,
        "db_id": app.db_open_paper_trade(symbol, {
            "strike": strike, "direction": direction, "entry_price": entry_price,
            "target_price": target_price, "sl_price": sl_price, "confidence": 70,
            "entry_time": "09:20:00", "entry_time_obj": app.now_ist(),
            "expiry_date_at_entry": expiry_date_at_entry,
        }),
    }
    return bucket


def _open_scalp_trade(symbol="NIFTY", strike=25000, direction="CE", entry_price=100.0,
                       target_price=115.0, sl_price=92.0, expiry_date_at_entry="2026-08-06"):
    bucket = app.scalp_paper_trade_bucket(symbol)
    bucket["open_trade"] = {
        "symbol": symbol, "strike": strike, "direction": direction,
        "entry_price": entry_price, "target_price": target_price, "sl_price": sl_price,
        "entry_time": "09:20:00", "entry_time_obj": app.now_ist(),
        "current_price": entry_price, "points_now": 0.0,
        "expiry_date_at_entry": expiry_date_at_entry,
        "db_id": app.db_open_scalp_paper_trade(symbol, {
            "strike": strike, "direction": direction, "entry_price": entry_price,
            "target_price": target_price, "sl_price": sl_price,
            "entry_time": "09:20:00", "entry_time_obj": app.now_ist(),
            "expiry_date_at_entry": expiry_date_at_entry,
        }),
    }
    return bucket


def _open_v3_trade(symbol="NIFTY", strike=25000, direction="CE", entry_price=100.0,
                    target_price=160.0, sl_price=80.0, expiry_date_at_entry="2026-08-06"):
    bucket = app.v3_paper_trade_bucket(symbol)
    bucket["open_trade"] = {
        "symbol": symbol, "strike": strike, "direction": direction,
        "entry_price": entry_price, "target_price": target_price, "sl_price": sl_price,
        "entry_time": "09:20:00", "entry_time_obj": app.now_ist(),
        "current_price": entry_price, "points_now": 0.0,
        "expiry_date_at_entry": expiry_date_at_entry,
        "db_id": app.db_open_v3_paper_trade(symbol, {
            "strike": strike, "direction": direction, "entry_price": entry_price,
            "target_price": target_price, "sl_price": sl_price,
            "entry_time": "09:20:00", "entry_time_obj": app.now_ist(),
            "expiry_date_at_entry": expiry_date_at_entry,
        }),
    }
    return bucket


class TestUpdatePaperTradingExpiryIdentity:
    """update_paper_trading() / paper_trades table (S-R Engine)."""

    def test_same_expiry_still_matches_normally(self, client):
        _open_sr_trade(expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"), patch.object(app, "send_telegram"):
            app.update_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=165.0, pe_ltp=None)], "11:00:00",
                expiry_date=dt.date(2026, 8, 6),
            )
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason, exit_price FROM paper_trades").fetchone()
        conn.close()
        assert row[0] == "CLOSED"
        assert row[1] == "TARGET HIT"
        assert row[2] == 165.0
        assert app.paper_trade_bucket("NIFTY")["open_trade"] is None

    def test_rollover_never_matches_the_new_contracts_price(self, client):
        """The new contract at the SAME strike is priced at 170.0 -- which
        would ALSO trigger this trade's target (>=160) if wrongly matched.
        The fix must use the pre-rollover price (120.0) instead."""
        _insert_cycle_and_strike(app.DB_PATH, symbol="NIFTY", ts="2026-08-06T15:25:00", strike=25000, ce_ltp=120.0)
        _open_sr_trade(entry_price=100.0, target_price=160.0, expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"), patch.object(app, "send_telegram"):
            app.update_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=170.0, pe_ltp=None)], "09:16:00",
                expiry_date=dt.date(2026, 8, 13),
            )
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason, exit_price FROM paper_trades").fetchone()
        conn.close()
        assert row[0] == "CLOSED"
        assert row[1].startswith("EXPIRED")
        assert row[2] == 120.0  # the OLD contract's last real price, never 170.0
        assert app.paper_trade_bucket("NIFTY")["open_trade"] is None

    def test_rollover_with_no_prior_history_holds_rather_than_fabricating(self, client):
        _open_sr_trade(strike=99999, entry_price=60.0, target_price=90.0, expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"), patch.object(app, "send_telegram"):
            app.update_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=100.0, pe_ltp=None)], "09:16:00",
                expiry_date=dt.date(2026, 8, 13),
            )
        assert app.paper_trade_bucket("NIFTY")["open_trade"] is not None
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status FROM paper_trades").fetchone()
        conn.close()
        assert row[0] == "OPEN"

    def test_no_expiry_date_passed_is_backward_compatible(self, client):
        _open_sr_trade(entry_price=100.0, target_price=160.0, expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"), patch.object(app, "send_telegram"):
            app.update_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=165.0, pe_ltp=None)], "11:00:00",
            )   # no expiry_date kwarg at all
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason FROM paper_trades").fetchone()
        conn.close()
        assert row[0] == "CLOSED"
        assert row[1] == "TARGET HIT"

    def test_missing_expiry_at_entry_is_backfilled_and_matches_normally_this_cycle(self, client):
        bucket = _open_sr_trade(entry_price=100.0, target_price=160.0, expiry_date_at_entry=None)
        db_id = bucket["open_trade"]["db_id"]
        with patch.object(app, "socketio"), patch.object(app, "send_telegram"):
            app.update_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=165.0, pe_ltp=None)], "11:00:00",
                expiry_date=dt.date(2026, 8, 6),
            )
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason FROM paper_trades WHERE id=?", (db_id,)).fetchone()
        conn.close()
        assert row[0] == "CLOSED"
        assert row[1] == "TARGET HIT"

    def test_backfill_never_overwrites_a_real_value(self, client):
        """Backfill only fires when expiry_date_at_entry is NULL -- a trade
        that already has a real value keeps it, even if it happens to differ
        from this cycle's resolved expiry (that's exactly the rollover case,
        handled separately, not silently overwritten)."""
        _open_sr_trade(entry_price=60.0, target_price=200.0, sl_price=10.0,
                        expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"), patch.object(app, "send_telegram"):
            app.update_paper_trading(
                "NIFTY", None, [], "09:16:00", expiry_date=dt.date(2026, 8, 13),
            )
        ot = app.paper_trade_bucket("NIFTY")["open_trade"]
        assert ot is not None
        assert ot["expiry_date_at_entry"] == "2026-08-06"


class TestUpdateScalpPaperTradingExpiryIdentity:
    """update_scalp_paper_trading() / scalp_paper_trades table."""

    def test_same_expiry_still_matches_normally(self, client):
        _open_scalp_trade(entry_price=100.0, target_price=115.0, expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"):
            app.update_scalp_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=120.0, pe_ltp=None)], "11:00:00",
                expiry_date=dt.date(2026, 8, 6),
            )
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason, exit_price FROM scalp_paper_trades").fetchone()
        conn.close()
        assert row[0] == "CLOSED"
        assert row[1] == "TARGET HIT"
        assert row[2] == 120.0

    def test_rollover_never_matches_the_new_contracts_price(self, client):
        _insert_cycle_and_strike(app.DB_PATH, symbol="NIFTY", ts="2026-08-06T15:25:00", strike=25000, ce_ltp=105.0)
        _open_scalp_trade(entry_price=100.0, target_price=115.0, expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"):
            app.update_scalp_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=130.0, pe_ltp=None)], "09:16:00",
                expiry_date=dt.date(2026, 8, 13),
            )
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason, exit_price FROM scalp_paper_trades").fetchone()
        conn.close()
        assert row[0] == "CLOSED"
        assert row[1].startswith("EXPIRED")
        assert row[2] == 105.0

    def test_rollover_with_no_prior_history_holds_rather_than_fabricating(self, client):
        _open_scalp_trade(strike=99999, entry_price=60.0, target_price=90.0, expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"):
            app.update_scalp_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=100.0, pe_ltp=None)], "09:16:00",
                expiry_date=dt.date(2026, 8, 13),
            )
        assert app.scalp_paper_trade_bucket("NIFTY")["open_trade"] is not None

    def test_no_expiry_date_passed_is_backward_compatible(self, client):
        _open_scalp_trade(entry_price=100.0, target_price=115.0, expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"):
            app.update_scalp_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=120.0, pe_ltp=None)], "11:00:00",
            )
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason FROM scalp_paper_trades").fetchone()
        conn.close()
        assert row[0] == "CLOSED"
        assert row[1] == "TARGET HIT"


class TestUpdateV3PaperTradingExpiryIdentity:
    """update_v3_paper_trading() / v3_paper_trades table."""

    def test_same_expiry_still_matches_normally(self, client):
        _open_v3_trade(entry_price=100.0, target_price=160.0, expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"):
            app.update_v3_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=165.0, pe_ltp=None)], "11:00:00",
                expiry_date=dt.date(2026, 8, 6),
            )
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason, exit_price FROM v3_paper_trades").fetchone()
        conn.close()
        assert row[0] == "CLOSED"
        assert row[1] == "TARGET HIT"
        assert row[2] == 165.0

    def test_rollover_never_matches_the_new_contracts_price(self, client):
        _insert_cycle_and_strike(app.DB_PATH, symbol="NIFTY", ts="2026-08-06T15:25:00", strike=25000, ce_ltp=120.0)
        _open_v3_trade(entry_price=100.0, target_price=160.0, expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"):
            app.update_v3_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=170.0, pe_ltp=None)], "09:16:00",
                expiry_date=dt.date(2026, 8, 13),
            )
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason, exit_price FROM v3_paper_trades").fetchone()
        conn.close()
        assert row[0] == "CLOSED"
        assert row[1].startswith("EXPIRED")
        assert row[2] == 120.0

    def test_rollover_with_no_prior_history_holds_rather_than_fabricating(self, client):
        _open_v3_trade(strike=99999, entry_price=60.0, target_price=90.0, expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"):
            app.update_v3_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=100.0, pe_ltp=None)], "09:16:00",
                expiry_date=dt.date(2026, 8, 13),
            )
        assert app.v3_paper_trade_bucket("NIFTY")["open_trade"] is not None

    def test_no_expiry_date_passed_is_backward_compatible(self, client):
        _open_v3_trade(entry_price=100.0, target_price=160.0, expiry_date_at_entry="2026-08-06")
        with patch.object(app, "socketio"):
            app.update_v3_paper_trading(
                "NIFTY", None, [StrikeRow(strike=25000, ce_ltp=165.0, pe_ltp=None)], "11:00:00",
            )
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason FROM v3_paper_trades").fetchone()
        conn.close()
        assert row[0] == "CLOSED"
        assert row[1] == "TARGET HIT"


class TestSchemaMigration:
    def test_all_three_tables_have_expiry_column(self, client):
        for tbl in ("paper_trades", "scalp_paper_trades", "v3_paper_trades"):
            conn = sqlite3.connect(app.DB_PATH)
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})")}
            conn.close()
            assert "expiry_date_at_entry" in cols, f"{tbl} missing expiry_date_at_entry"

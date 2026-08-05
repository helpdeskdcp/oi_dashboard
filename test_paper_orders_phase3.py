"""
test_paper_orders_phase3.py -- regression tests for Phase 3 of paper trading:
new order types (STOP/BRACKET/COVER), generalized trailing stop, intraday
forced square-off, and the per-user AI Auto-Trading fan-out
(fanout_auto_trade_entry / select_best_scalp_candidate / user_auto_trading_settings).

Same technique as test_manual_trading.py: SKIP_AUTOSTART=1 before importing
app, then point app.DB_PATH / auth.DB_PATH / billing.DB_PATH at a throwaway
file per test and call app.init_db() explicitly.
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
from oi_engine import StrikeRow

NIFTY_CFG = app.SYMBOLS["NIFTY"]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    app.init_db()
    app.state["auto_fanout_cooldown"].clear()   # module-level dict -- avoid cross-test bleed
    app.state.get("sr_active_profile_cache", {}).clear()   # ditto -- each test gets a fresh DB
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _seed_user(db_path, email=None, username=None, password="Testpass123", role="subscriber",
                is_verified=1, is_suspended=0, trial_ends_at="__default__", subscription_expires_at=None,
                wallet_balance=50000):
    if trial_ends_at == "__default__":
        trial_ends_at = (auth.now_ist() + dt.timedelta(days=15)).isoformat()
    conn = sqlite3.connect(db_path)
    now_str = auth.now_ist().isoformat()
    conn.execute(
        """INSERT INTO users (email, username, password_hash, role, is_verified, is_suspended,
                               trial_started_at, trial_ends_at, subscription_expires_at,
                               wallet_balance, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (email, username, auth.hash_password(password), role, is_verified, is_suspended,
         now_str if trial_ends_at else None, trial_ends_at, subscription_expires_at,
         wallet_balance, now_str, now_str),
    )
    conn.commit()
    user_id = conn.execute("SELECT id FROM users WHERE email=? OR username=?", (email, username)).fetchone()[0]
    conn.close()
    return user_id


def _login_session(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _csrf(client, token="test-csrf-token-value"):
    with client.session_transaction() as sess:
        sess["csrf_token"] = token
    return token


def _set_live_payload(symbol, strike, ce_ltp=None, pe_ltp=None):
    app.state["last_payload_by_symbol"][symbol] = {
        "atm": strike, "ltp": strike,
        "rows": [{"strike": strike, "ce_ltp": ce_ltp, "pe_ltp": pe_ltp}],
    }


def _seed_order(db_path, user_id, symbol="NIFTY", strike=25000, direction="CE",
                 status="OPEN", entry_price=100.0, qty=1, target_price=None, sl_price=None,
                 wallet_linked=1, order_type="MARKET", limit_price=None, stop_price=None,
                 trade_source="MANUAL", source_engine=None, intraday_only=0,
                 trailing_stop_enabled=0, trailing_trigger_pct=None, trailing_giveback_pct=None,
                 breakeven_trigger_pct=None, peak_price=None, sl_trailed=0, entry_ts=None):
    conn = sqlite3.connect(db_path)
    now = auth.now_ist()
    cur = conn.execute(
        """INSERT INTO paper_orders (user_id, symbol, strike, direction, trade_source, source_engine,
                                      order_type, limit_price, stop_price, entry_price, target_price, sl_price,
                                      qty, entry_time, entry_ts, status, wallet_linked, intraday_only,
                                      trailing_stop_enabled, trailing_trigger_pct, trailing_giveback_pct,
                                      breakeven_trigger_pct, peak_price, sl_trailed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, symbol, strike, direction, trade_source, source_engine, order_type, limit_price, stop_price,
         entry_price, target_price, sl_price, qty, now.strftime("%H:%M:%S"),
         entry_ts if entry_ts is not None else now.timestamp(), status, wallet_linked, intraday_only,
         trailing_stop_enabled, trailing_trigger_pct, trailing_giveback_pct, breakeven_trigger_pct,
         peak_price, sl_trailed),
    )
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return order_id


class TestOrderTypeValidation:
    def test_stop_requires_stop_price(self, client):
        uid = _seed_user(app.DB_PATH, email="stopval@test.com")
        _login_session(client, uid)
        token = _csrf(client)
        resp = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 1,
            "order_type": "STOP", "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert resp.status_code == 400
        assert "stop_price" in resp.get_json()["error"]

    def test_bracket_requires_target_and_sl(self, client):
        uid = _seed_user(app.DB_PATH, email="bracketval@test.com")
        _login_session(client, uid)
        token = _csrf(client)
        _set_live_payload("NIFTY", 25000, ce_ltp=100.0)

        missing_both = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 1,
            "order_type": "BRACKET", "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert missing_both.status_code == 400

        missing_sl = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 1,
            "order_type": "BRACKET", "target_price": 120, "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert missing_sl.status_code == 400

        ok = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 1,
            "order_type": "BRACKET", "target_price": 120, "sl_price": 90, "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert ok.status_code == 200

        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT order_type, intraday_only, status FROM paper_orders WHERE user_id=?", (uid,)).fetchone()
        conn.close()
        assert row == ("BRACKET", 1, "OPEN")

    def test_cover_requires_sl_but_not_target(self, client):
        uid = _seed_user(app.DB_PATH, email="coverval@test.com")
        _login_session(client, uid)
        token = _csrf(client)
        _set_live_payload("NIFTY", 25000, ce_ltp=100.0)

        missing_sl = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 1,
            "order_type": "COVER", "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert missing_sl.status_code == 400

        ok = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 1,
            "order_type": "COVER", "sl_price": 90, "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert ok.status_code == 200

        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT order_type, intraday_only, target_price, status FROM paper_orders WHERE user_id=?", (uid,)).fetchone()
        conn.close()
        assert row == ("COVER", 1, None, "OPEN")

    def test_trailing_requires_target(self, client):
        uid = _seed_user(app.DB_PATH, email="trailval@test.com")
        _login_session(client, uid)
        token = _csrf(client)
        _set_live_payload("NIFTY", 25000, ce_ltp=100.0)
        resp = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 1,
            "order_type": "MARKET", "trailing_stop_enabled": True, "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert resp.status_code == 400


class TestStopOrderFill:
    def test_stays_pending_then_fills_on_breakout(self, client):
        uid = _seed_user(app.DB_PATH, email="stopfill@test.com", wallet_balance=10000)
        order_id = _seed_order(app.DB_PATH, uid, status="PENDING", entry_price=None,
                                order_type="STOP", stop_price=95.0)

        # Below trigger -- stays PENDING.
        with patch.object(app, "socketio"):
            app.update_paper_orders("NIFTY", [StrikeRow(strike=25000, ce_ltp=94.0, pe_ltp=None)], "10:00:00", NIFTY_CFG)
        conn = sqlite3.connect(app.DB_PATH)
        status = conn.execute("SELECT status FROM paper_orders WHERE id=?", (order_id,)).fetchone()[0]
        conn.close()
        assert status == "PENDING"

        # Crosses trigger -- fills at current price (not stop_price).
        with patch.object(app, "socketio"):
            app.update_paper_orders("NIFTY", [StrikeRow(strike=25000, ce_ltp=97.0, pe_ltp=None)], "10:05:00", NIFTY_CFG)
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, entry_price FROM paper_orders WHERE id=?", (order_id,)).fetchone()
        balance = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        conn.close()
        assert row == ("OPEN", 97.0)
        assert balance == 10000 - 97.0


class TestIntradaySquareOff:
    def test_fires_inside_buffer_not_before(self, client):
        uid = _seed_user(app.DB_PATH, email="squareoff@test.com", wallet_balance=0)
        order_id = _seed_order(app.DB_PATH, uid, entry_price=100.0, qty=1,
                                order_type="COVER", sl_price=80.0, intraday_only=1)

        oh, om, ch, cm = app.MARKET_HOURS[NIFTY_CFG["type"]]
        today = dt.datetime(2026, 7, 27)   # a Monday
        close_t = today.replace(hour=ch, minute=cm)

        # 20 minutes before close (outside the 5-minute buffer) -- must NOT square off.
        before_buffer = close_t - dt.timedelta(minutes=20)
        with patch.object(app, "now_ist", lambda: before_buffer), patch.object(app, "socketio"):
            app.update_paper_orders("NIFTY", [StrikeRow(strike=25000, ce_ltp=105.0, pe_ltp=None)], "15:10:00", NIFTY_CFG)
        conn = sqlite3.connect(app.DB_PATH)
        status = conn.execute("SELECT status FROM paper_orders WHERE id=?", (order_id,)).fetchone()[0]
        conn.close()
        assert status == "OPEN"

        # 2 minutes before close (inside the 5-minute buffer) -- must square off.
        inside_buffer = close_t - dt.timedelta(minutes=2)
        with patch.object(app, "now_ist", lambda: inside_buffer), patch.object(app, "socketio"):
            app.update_paper_orders("NIFTY", [StrikeRow(strike=25000, ce_ltp=105.0, pe_ltp=None)], "15:28:00", NIFTY_CFG)
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason FROM paper_orders WHERE id=?", (order_id,)).fetchone()
        balance = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        conn.close()
        assert row == ("CLOSED", "SQUARE-OFF")
        assert balance == 105.0   # full exit proceeds credited


class TestGeneralizedTrailingStop:
    def test_ratchets_up_never_down_then_exits_trailing_sl(self, client):
        uid = _seed_user(app.DB_PATH, email="trail@test.com", wallet_balance=0)
        order_id = _seed_order(app.DB_PATH, uid, entry_price=100.0, qty=1,
                                order_type="BRACKET", target_price=120.0, sl_price=90.0,
                                intraday_only=1, trailing_stop_enabled=1)

        fixed_now = dt.datetime(2026, 7, 27, 11, 0, 0)   # well clear of the close-buffer window

        def run(ce_ltp):
            with patch.object(app, "now_ist", lambda: fixed_now), patch.object(app, "socketio"):
                app.update_paper_orders("NIFTY", [StrikeRow(strike=25000, ce_ltp=ce_ltp, pe_ltp=None)],
                                         fixed_now.strftime("%H:%M:%S"), NIFTY_CFG)
            conn = sqlite3.connect(app.DB_PATH)
            row = conn.execute("SELECT sl_price, sl_trailed, peak_price, status, exit_reason, points "
                                "FROM paper_orders WHERE id=?", (order_id,)).fetchone()
            conn.close()
            return row

        # 30% progress (6/20) -- breakeven stage: SL -> entry (100).
        sl, trailed, peak, status, _, _ = run(106.0)
        assert (sl, trailed, status) == (100.0, 1, "OPEN")

        # 70% progress (14/20, peak 114) -- trail stage: SL -> 100 + 14*0.7 = 109.8.
        sl, trailed, peak, status, _, _ = run(114.0)
        assert (sl, trailed, peak, status) == (109.8, 1, 114.0, "OPEN")

        # Retrace above the new trailing SL -- must stay open, SL must NOT move down.
        sl, trailed, peak, status, _, _ = run(110.0)
        assert (sl, status) == (109.8, "OPEN")

        # Retrace below the trailing SL -- exits TRAILING SL (not STOP LOSS), full proceeds credited.
        sl, trailed, peak, status, exit_reason, points = run(109.0)
        assert (status, exit_reason, points) == ("CLOSED", "TRAILING SL", 9.0)

        conn = sqlite3.connect(app.DB_PATH)
        balance = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        conn.close()
        assert balance == 109.0


class TestAutoSwingTimeExit:
    """Phase 4 gap fix: AUTO/SWING paper_orders previously had no time-exit
    at all -- they'd sit open indefinitely waiting for target/SL even though
    the engine's own reference trade (update_paper_trading) already
    time-exits on max_hold_minutes. Manual orders are untouched (no
    source_engine), matching update_paper_orders' documented trade_source-
    blind design -- source_engine, not trade_source, is what's keyed on."""

    def test_swing_auto_order_time_exits_past_max_hold_minutes(self, client):
        uid = _seed_user(app.DB_PATH, email="timeexit@test.com", wallet_balance=0)
        max_hold = app.ENGINE_PARAM_SPECS["sr"]["max_hold_minutes"]["default"]
        stale_entry_ts = auth.now_ist().timestamp() - (max_hold + 1) * 60
        order_id = _seed_order(app.DB_PATH, uid, entry_price=100.0, qty=1,
                                target_price=120.0, sl_price=90.0,
                                trade_source="AUTO", source_engine="SWING", entry_ts=stale_entry_ts)

        with patch.object(app, "socketio"):
            app.update_paper_orders("NIFTY", [StrikeRow(strike=25000, ce_ltp=105.0, pe_ltp=None)],
                                     auth.now_ist().strftime("%H:%M:%S"), NIFTY_CFG)

        conn = sqlite3.connect(app.DB_PATH)
        status, exit_reason = conn.execute(
            "SELECT status, exit_reason FROM paper_orders WHERE id=?", (order_id,)).fetchone()
        conn.close()
        assert (status, exit_reason) == ("CLOSED", "TIME EXIT")

    def test_manual_order_never_time_exits(self, client):
        """No source_engine -> no tuned max_hold_minutes to key off of --
        manual orders keep their pre-existing behavior of never force-
        exiting on hold time."""
        uid = _seed_user(app.DB_PATH, email="manual-nohold@test.com", wallet_balance=0)
        max_hold = app.ENGINE_PARAM_SPECS["sr"]["max_hold_minutes"]["default"]
        stale_entry_ts = auth.now_ist().timestamp() - (max_hold + 60) * 60   # way past max_hold
        order_id = _seed_order(app.DB_PATH, uid, entry_price=100.0, qty=1,
                                target_price=120.0, sl_price=90.0,
                                trade_source="MANUAL", source_engine=None, entry_ts=stale_entry_ts)

        with patch.object(app, "socketio"):
            app.update_paper_orders("NIFTY", [StrikeRow(strike=25000, ce_ltp=105.0, pe_ltp=None)],
                                     auth.now_ist().strftime("%H:%M:%S"), NIFTY_CFG)

        conn = sqlite3.connect(app.DB_PATH)
        status = conn.execute("SELECT status FROM paper_orders WHERE id=?", (order_id,)).fetchone()[0]
        conn.close()
        assert status == "OPEN"


class TestAutoFanout:
    def test_happy_path_multiple_users_different_qty(self, client):
        u_a = _seed_user(app.DB_PATH, email="fanoutA@test.com", wallet_balance=50000)
        u_b = _seed_user(app.DB_PATH, email="fanoutB@test.com", wallet_balance=50000)
        u_c = _seed_user(app.DB_PATH, email="fanoutC@test.com", wallet_balance=50000)   # not enrolled

        now_str = auth.now_ist().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        conn.execute("INSERT INTO user_auto_trading_settings (user_id, engine, enabled, qty, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?)", (u_a, "SWING", 1, 2, now_str, now_str))
        conn.execute("INSERT INTO user_auto_trading_settings (user_id, engine, enabled, qty, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?)", (u_b, "SWING", 1, 5, now_str, now_str))
        conn.commit()
        conn.close()

        trigger = {"strike": 25000, "direction": "CE", "entry_price": 100.0, "target_price": 120.0, "sl_price": 90.0}
        with patch.object(app, "socketio"):
            app.fanout_auto_trade_entry("SWING", "NIFTY", NIFTY_CFG, trigger, auth.now_ist())

        conn = sqlite3.connect(app.DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = {r["user_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM paper_orders WHERE source_engine='SWING'")}
        bal_a = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (u_a,)).fetchone()[0]
        bal_b = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (u_b,)).fetchone()[0]
        bal_c = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (u_c,)).fetchone()[0]
        conn.close()

        # user_auto_trading_settings.qty is LOTS (same convention as Manual
        # Trading) -- fanout_auto_trade_entry multiplies by NIFTY's own
        # LOT_SIZES entry (65) to get the actual order quantity.
        lot_size = app.LOT_SIZES["NIFTY"]
        assert set(rows.keys()) == {u_a, u_b}
        assert rows[u_a]["trade_source"] == "AUTO" and rows[u_a]["qty"] == 2 * lot_size
        assert rows[u_b]["trade_source"] == "AUTO" and rows[u_b]["qty"] == 5 * lot_size
        assert bal_a == 50000 - 100.0 * 2 * lot_size
        assert bal_b == 50000 - 100.0 * 5 * lot_size
        assert bal_c == 50000   # never enrolled -- completely untouched

    def test_insufficient_balance_skips_only_that_user(self, client):
        u_rich = _seed_user(app.DB_PATH, email="rich@test.com", wallet_balance=50000)
        u_poor = _seed_user(app.DB_PATH, email="poor@test.com", wallet_balance=10)

        now_str = auth.now_ist().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        conn.execute("INSERT INTO user_auto_trading_settings (user_id, engine, enabled, qty, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?)", (u_rich, "SWING", 1, 1, now_str, now_str))
        conn.execute("INSERT INTO user_auto_trading_settings (user_id, engine, enabled, qty, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?)", (u_poor, "SWING", 1, 1, now_str, now_str))
        conn.commit()
        conn.close()

        trigger = {"strike": 25000, "direction": "CE", "entry_price": 100.0, "target_price": 120.0, "sl_price": 90.0}
        with patch.object(app, "socketio"):
            app.fanout_auto_trade_entry("SWING", "NIFTY", NIFTY_CFG, trigger, auth.now_ist())

        conn = sqlite3.connect(app.DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = {r["user_id"]: dict(r) for r in conn.execute("SELECT * FROM paper_orders WHERE source_engine='SWING'")}
        conn.close()
        assert set(rows.keys()) == {u_rich}   # poor user skipped, rich user's fill unaffected

    def test_swing_orders_stamp_tuned_trailing_params_scalp_does_not(self, client):
        """Phase 4 gap fix: SWING AUTO orders previously opened with
        trailing_stop_enabled=0, so tuned trail/breakeven params never
        applied to a subscriber's own wallet trade (only to the engine's
        system-wide reference trade). They must now be stamped from
        get_sr_live_params at fan-out time. SCALP is untouched -- it has no
        backtest_profiles tuning."""
        u_swing = _seed_user(app.DB_PATH, email="swing-trail@test.com", wallet_balance=50000)
        u_scalp = _seed_user(app.DB_PATH, email="scalp-trail@test.com", wallet_balance=50000)
        now_str = auth.now_ist().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        conn.execute("INSERT INTO user_auto_trading_settings (user_id, engine, enabled, qty, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?)", (u_swing, "SWING", 1, 1, now_str, now_str))
        conn.execute("INSERT INTO user_auto_trading_settings (user_id, engine, enabled, qty, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?)", (u_scalp, "SCALP", 1, 1, now_str, now_str))
        conn.commit()
        conn.close()

        trigger = {"strike": 25000, "direction": "CE", "entry_price": 100.0, "target_price": 120.0, "sl_price": 90.0}
        with patch.object(app, "socketio"):
            app.fanout_auto_trade_entry("SWING", "NIFTY", NIFTY_CFG, trigger, auth.now_ist())
            app.fanout_auto_trade_entry("SCALP", "NIFTY", NIFTY_CFG, trigger, auth.now_ist())

        conn = sqlite3.connect(app.DB_PATH)
        conn.row_factory = sqlite3.Row
        swing_row = conn.execute("SELECT * FROM paper_orders WHERE user_id=?", (u_swing,)).fetchone()
        scalp_row = conn.execute("SELECT * FROM paper_orders WHERE user_id=?", (u_scalp,)).fetchone()
        conn.close()

        defaults = app.ENGINE_PARAM_SPECS["sr"]
        assert swing_row["trailing_stop_enabled"] == 1
        assert swing_row["trailing_trigger_pct"] == defaults["trail_trigger_pct"]["default"]
        assert swing_row["trailing_giveback_pct"] == defaults["trail_giveback_pct"]["default"]
        assert swing_row["breakeven_trigger_pct"] == defaults["breakeven_trigger_pct"]["default"]
        assert scalp_row["trailing_stop_enabled"] == 0
        assert scalp_row["trailing_trigger_pct"] is None

    def test_dedup_guard_prevents_duplicate_order_same_cycle(self, client):
        # Regression test for the non-edge-triggered Scalp flaw: without the
        # OPEN/PENDING dedup guard, calling fanout twice with the same
        # tradeable candidate (exactly what happens every cycle Scalp's
        # tradeable flag stays true) would open a SECOND order for the same
        # user/symbol/engine.
        uid = _seed_user(app.DB_PATH, email="dedup@test.com", wallet_balance=50000)
        now_str = auth.now_ist().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        conn.execute("INSERT INTO user_auto_trading_settings (user_id, engine, enabled, qty, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?)", (uid, "SCALP", 1, 1, now_str, now_str))
        conn.commit()
        conn.close()

        candidate = {"strike": 25000, "direction": "CE", "entry_price": 50.0, "target_price": 55.0, "sl_price": 47.0}
        with patch.object(app, "socketio"):
            app.fanout_auto_trade_entry("SCALP", "NIFTY", NIFTY_CFG, candidate, auth.now_ist())
            app.fanout_auto_trade_entry("SCALP", "NIFTY", NIFTY_CFG, candidate, auth.now_ist())

        conn = sqlite3.connect(app.DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM paper_orders WHERE user_id=? AND source_engine='SCALP'", (uid,)).fetchone()[0]
        conn.close()
        assert count == 1


class TestSelectBestScalpCandidate:
    def test_none_when_nothing_tradeable(self):
        assert app.select_best_scalp_candidate(None) is None
        assert app.select_best_scalp_candidate({"CE": {"tradeable": False}, "PE": None}) is None

    def test_picks_higher_risk_reward_when_both_tradeable(self):
        signal = {
            "CE": {"tradeable": True, "risk_reward": 1.5, "strike": 25000, "direction": "CE"},
            "PE": {"tradeable": True, "risk_reward": 2.5, "strike": 25000, "direction": "PE"},
        }
        best = app.select_best_scalp_candidate(signal)
        assert best["direction"] == "PE"


class TestTradeSourceContract:
    def test_my_trades_never_emits_a_third_trade_source_value(self, client):
        uid = _seed_user(app.DB_PATH, email="contract@test.com", wallet_balance=50000)
        _seed_order(app.DB_PATH, uid, status="OPEN", trade_source="MANUAL")
        _seed_order(app.DB_PATH, uid, status="OPEN", trade_source="AUTO", source_engine="SWING")
        _seed_order(app.DB_PATH, uid, status="CLOSED", trade_source="AUTO", source_engine="SCALP",
                    entry_price=50.0, target_price=55.0)
        _seed_order(app.DB_PATH, uid, status="PENDING", entry_price=None, order_type="LIMIT",
                    limit_price=90.0, trade_source="MANUAL")

        _login_session(client, uid)
        resp = client.get("/api/manual-trade/my-trades")
        assert resp.status_code == 200
        data = resp.get_json()
        all_rows = data["open"] + data["pending"] + data["closed"]
        assert len(all_rows) == 4
        assert all(r["trade_source"] in ("MANUAL", "AUTO") for r in all_rows)

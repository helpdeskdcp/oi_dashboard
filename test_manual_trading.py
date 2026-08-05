"""
test_manual_trading.py -- regression tests for Phase 2 of Manual Trading:
per-user partitioning, limit orders, auto target/SL exit, and wallet-linked
P&L (app.py's manual-trade routes + update_paper_orders, and
billing.debit_if_sufficient). Table was renamed manual_paper_trades ->
paper_orders in Phase 3 (see test_paper_orders_phase3.py for the new
order-type/trailing-stop/AUTO-fanout coverage) -- these tests are the
regression backstop proving that rename didn't break any Phase 2 behavior.

Same technique as test_auth.py: SKIP_AUTOSTART=1 before importing app, then
point app.DB_PATH / auth.DB_PATH / billing.DB_PATH at a throwaway file per
test and call app.init_db() explicitly.
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


def _seed_trade(db_path, user_id, symbol="NIFTY", strike=25000, direction="CE",
                 status="OPEN", entry_price=100.0, qty=65, target_price=None, sl_price=None,
                 wallet_linked=1, order_type="MARKET", limit_price=None, stop_price=None,
                 trade_source="MANUAL", source_engine=None, intraday_only=0):
    conn = sqlite3.connect(db_path)
    now_str = auth.now_ist().isoformat()
    cur = conn.execute(
        """INSERT INTO paper_orders (user_id, symbol, strike, direction, trade_source, source_engine,
                                      order_type, limit_price, stop_price, entry_price, target_price, sl_price,
                                      qty, entry_time, entry_ts, status, wallet_linked, intraday_only)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, symbol, strike, direction, trade_source, source_engine, order_type, limit_price, stop_price,
         entry_price, target_price, sl_price, qty, now_str, auth.now_ist().timestamp(), status, wallet_linked,
         intraday_only),
    )
    conn.commit()
    trade_id = cur.lastrowid
    conn.close()
    return trade_id


def _set_live_payload(symbol, strike, ce_ltp=None, pe_ltp=None):
    app.state["last_payload_by_symbol"][symbol] = {
        "atm": strike, "ltp": strike,
        "rows": [{"strike": strike, "ce_ltp": ce_ltp, "pe_ltp": pe_ltp}],
    }


class TestMigration:
    def test_legacy_row_backfills_to_admin_wallet_unlinked(self, monkeypatch, tmp_path):
        # Build a genuinely PRE-Phase-2 table (old column set only, no
        # user_id/order_type/limit_price/wallet_linked, and still named
        # manual_paper_trades -- pre-Phase-3 too) and a pre-existing admin
        # user, THEN run init_db() for the first time against it -- this is
        # the real scenario the migration branch has to handle (a live
        # upgrade all the way from pre-Phase-2 to current), unlike the
        # `client` fixture which always starts from a fully-migrated fresh
        # schema.
        db_path = str(tmp_path / "legacy.db")
        monkeypatch.setattr(app, "DB_PATH", db_path)
        monkeypatch.setattr(auth, "DB_PATH", db_path)
        monkeypatch.setattr(billing, "DB_PATH", db_path)
        monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "")
        monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "")

        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE manual_paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, strike INTEGER, direction TEXT,
                entry_price REAL, target_price REAL, sl_price REAL, qty INTEGER DEFAULT 1,
                entry_time TEXT, entry_ts REAL,
                exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL,
                status TEXT DEFAULT 'OPEN', trader_note TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO manual_paper_trades (symbol, strike, direction, entry_price, qty, status) "
            "VALUES ('NIFTY', 25000, 'CE', 100.0, 1, 'OPEN')"
        )
        conn.commit()
        conn.close()
        # NOTE: deliberately no `users` table / admin row set up here -- a
        # truly pre-Phase-1 DB wouldn't have one either, and init_db() must
        # create it (with no bootstrap admin, since credentials are blank).

        app.init_db()   # creates users table (no bootstrap admin) + migrates manual_paper_trades -> paper_orders (Phase 2 + Phase 3 columns)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT user_id, wallet_linked, trade_source FROM paper_orders WHERE symbol='NIFTY'").fetchone()
        admin_row = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert admin_row is None   # no bootstrap credentials given -- confirms this test's premise
        assert row[0] is None      # backfill target was None since no admin existed -- exercises that exact branch
        assert row[1] == 0         # still correctly flagged wallet_linked=0 regardless
        assert row[2] == "MANUAL"  # Phase 3 default -- every pre-Phase-3 row genuinely was manual
        assert "manual_paper_trades" not in tables
        assert "paper_orders" in tables

    def test_legacy_row_backfills_to_admin_when_one_exists(self, client):
        # Same idea, but this time an admin DOES exist (the `client` fixture's
        # bootstrap admin), and the DB is already at the CURRENT (Phase 3)
        # schema -- simulate the manual_paper_trades table reappearing in its
        # OLD (pre-Phase-2) shape (bypassing the app's own code, same as a
        # real legacy-upgrade scenario would), then re-run init_db() to
        # exercise the FULL migration cascade (rename + Phase 2 columns +
        # Phase 3 columns) from scratch against a DB that otherwise already
        # has the rest of Phase 3's schema (users, wallet_transactions, etc).
        conn = sqlite3.connect(app.DB_PATH)
        conn.execute("ALTER TABLE paper_orders RENAME TO paper_orders_old")
        conn.execute(
            """CREATE TABLE manual_paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, strike INTEGER, direction TEXT,
                entry_price REAL, target_price REAL, sl_price REAL, qty INTEGER DEFAULT 1,
                entry_time TEXT, entry_ts REAL,
                exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL,
                status TEXT DEFAULT 'OPEN', trader_note TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO manual_paper_trades (symbol, strike, direction, entry_price, qty, status) "
            "VALUES ('NIFTY', 25000, 'CE', 100.0, 1, 'OPEN')"
        )
        conn.execute("DROP TABLE paper_orders_old")
        conn.commit()
        conn.close()

        app.init_db()
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT user_id, wallet_linked, trade_source FROM paper_orders WHERE symbol='NIFTY'").fetchone()
        admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
        conn.close()
        assert row[0] == admin_id
        assert row[1] == 0
        assert row[2] == "MANUAL"


class TestOwnership:
    def test_cannot_exit_or_edit_or_cancel_other_users_trade(self, client):
        uid_a = _seed_user(app.DB_PATH, email="a@test.com")
        uid_b = _seed_user(app.DB_PATH, email="b@test.com")
        trade_id = _seed_trade(app.DB_PATH, uid_a)
        pending_id = _seed_trade(app.DB_PATH, uid_a, status="PENDING", entry_price=None,
                                  order_type="LIMIT", limit_price=90.0)

        _login_session(client, uid_b)
        token = _csrf(client)
        r1 = client.post("/api/manual-trade/exit", json={"id": trade_id, "csrf_token": token},
                          headers={"X-CSRFToken": token})
        r2 = client.post("/api/manual-trade/edit", json={"id": trade_id, "target_price": 200, "csrf_token": token},
                          headers={"X-CSRFToken": token})
        r3 = client.post("/api/manual-trade/cancel", json={"id": pending_id, "csrf_token": token},
                          headers={"X-CSRFToken": token})
        assert r1.status_code == 404
        assert r2.status_code == 404
        assert r3.status_code == 404

        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status FROM paper_orders WHERE id=?", (trade_id,)).fetchone()
        wallet = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (uid_a,)).fetchone()
        conn.close()
        assert row[0] == "OPEN"
        assert wallet[0] == 50000   # untouched


class TestSubscriptionGate:
    def test_lapsed_subscriber_blocked(self, client):
        uid = _seed_user(app.DB_PATH, email="lapsed@test.com",
                          trial_ends_at=(auth.now_ist() - dt.timedelta(days=1)).isoformat())
        _login_session(client, uid)
        resp = client.get("/manual-trading")
        assert resp.status_code == 302
        assert "/access-restricted" in resp.headers["Location"]

        token = _csrf(client)
        resp2 = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 65, "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert resp2.status_code == 302


class TestWalletDebitMarketEntry:
    def test_exact_balance_succeeds_short_balance_rejected(self, client):
        uid = _seed_user(app.DB_PATH, email="trader@test.com", wallet_balance=6500)
        _login_session(client, uid)
        token = _csrf(client)
        _set_live_payload("NIFTY", 25000, ce_ltp=100.0)

        resp = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 65,
            "target_price": 110, "sl_price": 90, "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["wallet_balance"] == 0.0

        conn = sqlite3.connect(app.DB_PATH)
        trade_count = conn.execute("SELECT COUNT(*) FROM paper_orders WHERE user_id=?", (uid,)).fetchone()[0]
        conn.close()
        assert trade_count == 1

        # Now broke -- a second entry must be rejected, no new row/ledger entry.
        resp2 = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 1, "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert resp2.status_code == 400
        assert "recharge" in resp2.get_json()["error"].lower() or "insufficient" in resp2.get_json()["error"].lower()

        conn = sqlite3.connect(app.DB_PATH)
        trade_count2 = conn.execute("SELECT COUNT(*) FROM paper_orders WHERE user_id=?", (uid,)).fetchone()[0]
        conn.close()
        assert trade_count2 == 1   # still just the one


class TestDoubleSpendRace:
    def test_second_concurrent_debit_fails_cleanly(self, client):
        uid = _seed_user(app.DB_PATH, email="race@test.com", wallet_balance=1000)
        first = billing.debit_if_sufficient(uid, 1000, "trade_entry", note="first")
        second = billing.debit_if_sufficient(uid, 1000, "trade_entry", note="second")
        assert first == 0.0
        assert second is None   # NOT a clamped partial success

        conn = sqlite3.connect(app.DB_PATH)
        balance = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        tx_count = conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE user_id=?", (uid,)).fetchone()[0]
        conn.close()
        assert balance == 0.0
        assert tx_count == 1   # only the first debit was ever recorded


class TestDoubleExitRace:
    def test_second_close_attempt_is_a_noop(self, client):
        uid = _seed_user(app.DB_PATH, email="dbl@test.com", wallet_balance=0)
        trade_id = _seed_trade(app.DB_PATH, uid, entry_price=100.0, qty=65, wallet_linked=1)

        conn = sqlite3.connect(app.DB_PATH)
        cur1 = conn.execute(
            "UPDATE paper_orders SET exit_price=?, exit_reason='TARGET HIT', points=?, status='CLOSED' "
            "WHERE id=? AND status='OPEN'", (110.0, 10.0, trade_id),
        )
        conn.commit()
        cur2 = conn.execute(
            "UPDATE paper_orders SET exit_price=?, exit_reason='MANUAL EXIT', points=?, status='CLOSED' "
            "WHERE id=? AND status='OPEN'", (111.0, 11.0, trade_id),
        )
        conn.commit()
        conn.close()
        assert cur1.rowcount == 1
        assert cur2.rowcount == 0   # lost the race -- must not also credit the wallet

        # Only the winner's credit should ever be applied by caller code (both
        # api_manual_trade_exit and update_paper_orders gate the wallet call
        # behind rowcount, exercised end-to-end in the auto-exit test below).


class TestLimitOrderLifecycle:
    def test_stays_pending_then_fills_and_debits(self, client):
        uid = _seed_user(app.DB_PATH, email="limit@test.com", wallet_balance=10000)
        _login_session(client, uid)
        token = _csrf(client)

        resp = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 65,
            "order_type": "LIMIT", "limit_price": 90.0, "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert resp.status_code == 200
        assert resp.get_json()["order_type"] == "LIMIT"

        conn = sqlite3.connect(app.DB_PATH)
        pending = conn.execute("SELECT id, status, user_id FROM paper_orders WHERE symbol='NIFTY'").fetchone()
        conn.close()
        assert pending[1] == "PENDING"

        # Price still above limit -- must not fill.
        rows_high = [StrikeRow(strike=25000, ce_ltp=100.0, pe_ltp=80.0)]
        with patch.object(app, "socketio") as mock_socketio:
            app.update_paper_orders("NIFTY", rows_high, "10:00:00", NIFTY_CFG)
        conn = sqlite3.connect(app.DB_PATH)
        still_pending = conn.execute("SELECT status FROM paper_orders WHERE id=?", (pending[0],)).fetchone()[0]
        conn.close()
        assert still_pending == "PENDING"

        # Price crosses limit -- must fill and debit at the ACTUAL fill price.
        rows_low = [StrikeRow(strike=25000, ce_ltp=88.0, pe_ltp=80.0)]
        with patch.object(app, "socketio") as mock_socketio:
            app.update_paper_orders("NIFTY", rows_low, "10:05:00", NIFTY_CFG)
        conn = sqlite3.connect(app.DB_PATH)
        filled = conn.execute("SELECT status, entry_price FROM paper_orders WHERE id=?", (pending[0],)).fetchone()
        balance = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        conn.close()
        assert filled[0] == "OPEN"
        assert filled[1] == 88.0
        assert balance == 10000 - 88.0 * 65
        mock_socketio.emit.assert_called()
        _, kwargs = mock_socketio.emit.call_args
        assert kwargs.get("room") == f"user_{uid}"


class TestAutoExit:
    def test_target_hit_and_stop_loss_credit_wallet(self, client):
        uid = _seed_user(app.DB_PATH, email="autoexit@test.com", wallet_balance=0)
        t1 = _seed_trade(app.DB_PATH, uid, entry_price=100.0, qty=65, target_price=110, sl_price=95)
        rows = [StrikeRow(strike=25000, ce_ltp=111.0, pe_ltp=80.0)]
        with patch.object(app, "socketio"):
            app.update_paper_orders("NIFTY", rows, "10:10:00", NIFTY_CFG)
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT status, exit_reason FROM paper_orders WHERE id=?", (t1,)).fetchone()
        balance = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        conn.close()
        assert row == ("CLOSED", "TARGET HIT")
        assert balance == round(111.0 * 65, 2)

        t2 = _seed_trade(app.DB_PATH, uid, entry_price=100.0, qty=65, target_price=110, sl_price=95)
        rows2 = [StrikeRow(strike=25000, ce_ltp=94.0, pe_ltp=80.0)]
        with patch.object(app, "socketio"):
            app.update_paper_orders("NIFTY", rows2, "10:15:00", NIFTY_CFG)
        conn = sqlite3.connect(app.DB_PATH)
        row2 = conn.execute("SELECT status, exit_reason FROM paper_orders WHERE id=?", (t2,)).fetchone()
        conn.close()
        assert row2 == ("CLOSED", "STOP LOSS")

    def test_legacy_unlinked_trade_auto_exits_without_wallet_touch(self, client):
        uid = _seed_user(app.DB_PATH, email="legacy@test.com", wallet_balance=12345)
        t = _seed_trade(app.DB_PATH, uid, entry_price=100.0, qty=65, target_price=110, wallet_linked=0)
        rows = [StrikeRow(strike=25000, ce_ltp=111.0, pe_ltp=80.0)]
        with patch.object(app, "socketio"):
            app.update_paper_orders("NIFTY", rows, "10:20:00", NIFTY_CFG)
        conn = sqlite3.connect(app.DB_PATH)
        status = conn.execute("SELECT status FROM paper_orders WHERE id=?", (t,)).fetchone()[0]
        balance = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        tx_count = conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE user_id=?", (uid,)).fetchone()[0]
        conn.close()
        assert status == "CLOSED"
        assert balance == 12345   # untouched
        assert tx_count == 0


class TestTargetSlValidation:
    def test_enter_rejects_inverted_target_and_sl(self, client):
        uid = _seed_user(app.DB_PATH, email="valid@test.com", wallet_balance=50000)
        _login_session(client, uid)
        token = _csrf(client)
        _set_live_payload("NIFTY", 25000, ce_ltp=100.0)

        bad_target = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 1,
            "target_price": 90, "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert bad_target.status_code == 400

        bad_sl = client.post("/api/manual-trade/enter", json={
            "symbol": "NIFTY", "strike": 25000, "direction": "CE", "qty": 1,
            "sl_price": 110, "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert bad_sl.status_code == 400

    def test_edit_rejects_inverted_values(self, client):
        uid = _seed_user(app.DB_PATH, email="valid2@test.com")
        trade_id = _seed_trade(app.DB_PATH, uid, entry_price=100.0)
        _login_session(client, uid)
        token = _csrf(client)
        resp = client.post("/api/manual-trade/edit", json={
            "id": trade_id, "target_price": 50, "csrf_token": token,
        }, headers={"X-CSRFToken": token})
        assert resp.status_code == 400


class TestCancel:
    def test_cancel_pending_no_wallet_touch(self, client):
        uid = _seed_user(app.DB_PATH, email="cancel@test.com", wallet_balance=50000)
        pending_id = _seed_trade(app.DB_PATH, uid, status="PENDING", entry_price=None,
                                  order_type="LIMIT", limit_price=90.0)
        _login_session(client, uid)
        token = _csrf(client)
        resp = client.post("/api/manual-trade/cancel", json={"id": pending_id, "csrf_token": token},
                            headers={"X-CSRFToken": token})
        assert resp.status_code == 200

        conn = sqlite3.connect(app.DB_PATH)
        status = conn.execute("SELECT status FROM paper_orders WHERE id=?", (pending_id,)).fetchone()[0]
        tx_count = conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE user_id=?", (uid,)).fetchone()[0]
        conn.close()
        assert status == "CANCELLED"
        assert tx_count == 0


class TestSocketPrivacy:
    def test_on_connect_joins_per_user_room(self, client):
        uid = _seed_user(app.DB_PATH, email="sock@test.com")
        with client.session_transaction() as sess:
            sess["user_id"] = uid
        with app.app.test_request_context("/socket.io/"):
            from flask import session as flask_session
            flask_session["user_id"] = uid
            with patch.object(app, "join_room") as mock_join, \
                 patch.object(app, "request") as mock_request, \
                 patch.object(app, "socketio"):
                mock_request.sid = "fake-sid-123"
                app.on_connect()
                mock_join.assert_called_once_with(f"user_{uid}", sid="fake-sid-123")

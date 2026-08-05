"""
test_auth.py -- regression tests for the accounts/roles/subscriptions/wallet
system (auth.py, billing.py, and the new routes in app.py).

Same technique as test_engine.py/test_scalping_engine.py: SKIP_AUTOSTART=1
before importing app (no live data threads, no real oi_history.db touched),
then point app.DB_PATH / auth.DB_PATH / billing.DB_PATH at a throwaway file
per test and call app.init_db() explicitly.
"""
import os
os.environ["SKIP_AUTOSTART"] = "1"

import time
import sqlite3
import tempfile
import datetime as dt

import pytest

import app
import auth
import billing


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    # Fast lockout window for the rate-limit test; harmless for the rest.
    monkeypatch.setattr(auth, "LOGIN_LOCKOUT_SECONDS", 2)
    auth._login_failures.clear()
    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _seed_user(db_path, email=None, username=None, password="Testpass123", role="subscriber",
                is_verified=1, is_suspended=0, trial_ends_at=None, subscription_expires_at=None,
                wallet_balance=50000):
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
    """Seeds a known CSRF token into the client's session and returns it, so
    a test can POST to a CSRF-protected endpoint (including the anonymous
    /register and /login forms, which are covered by the guard too -- see
    app.py's _csrf_guard) without needing to scrape a real GET-rendered page."""
    with client.session_transaction() as sess:
        sess["csrf_token"] = token
    return token


class TestBootstrapAdmin:
    def test_init_db_creates_bootstrap_admin_once(self, client):
        conn = sqlite3.connect(app.DB_PATH)
        admins = conn.execute("SELECT id, username FROM users WHERE role='admin'").fetchall()
        conn.close()
        assert len(admins) == 1
        assert admins[0][1] == "testadmin"

        # Calling init_db() again (as every restart does) must NOT create a second admin.
        app.init_db()
        conn = sqlite3.connect(app.DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        conn.close()
        assert count == 1


class TestRegisterVerifyLogin:
    def test_happy_path(self, client, monkeypatch):
        captured = {}

        def fake_send_email(to_email, subject, body_text):
            captured["to"] = to_email
            captured["body"] = body_text
            return True

        monkeypatch.setattr(auth, "send_email", fake_send_email)

        token = _csrf(client)
        resp = client.post("/register", data={
            "email": "newuser@test.com", "password": "GoodPass123", "confirm_password": "GoodPass123",
            "csrf_token": token,
        })
        assert resp.status_code == 200
        assert b"check your email" in resp.data.lower()
        assert "to" in captured, "send_email was never called during registration"

        # Never reveal the token in the public response.
        import re
        token_match = re.search(r"verify-email/([\w-]+)", captured["body"])
        assert token_match, "verification link missing from the email body"
        raw_token = token_match.group(1)
        assert raw_token not in resp.data.decode()

        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT is_verified, trial_started_at, trial_ends_at FROM users WHERE email=?",
                            ("newuser@test.com",)).fetchone()
        conn.close()
        assert row[0] == 0   # not verified yet
        assert row[1] is None and row[2] is None   # trial not started yet

        verify_resp = client.get(f"/verify-email/{raw_token}")
        assert verify_resp.status_code == 200
        assert b"trial" in verify_resp.data.lower()

        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute("SELECT is_verified, trial_started_at, trial_ends_at FROM users WHERE email=?",
                            ("newuser@test.com",)).fetchone()
        conn.close()
        assert row[0] == 1
        assert row[1] is not None and row[2] is not None

        login_resp = client.post("/login", data={"login_id": "newuser@test.com", "password": "GoodPass123",
                                                   "csrf_token": token})
        assert login_resp.status_code == 302

        home_resp = client.get("/")
        assert home_resp.status_code == 200


class TestRoleAndSubscriptionGating:
    def test_wrong_role_403(self, client):
        uid = _seed_user(app.DB_PATH, email="sub@test.com",
                          trial_ends_at=(auth.now_ist() + dt.timedelta(days=5)).isoformat())
        _login_session(client, uid)
        resp = client.get("/live-positions")
        assert resp.status_code == 403

    def test_anonymous_redirect_to_login(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_expired_subscription_blocks_subscriber(self, client):
        uid = _seed_user(app.DB_PATH, email="expired@test.com",
                          trial_ends_at=(auth.now_ist() - dt.timedelta(days=1)).isoformat())
        _login_session(client, uid)
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/access-restricted" in resp.headers["Location"]

    def test_active_subscription_allows_access(self, client):
        uid = _seed_user(app.DB_PATH, email="active@test.com",
                          trial_ends_at=(auth.now_ist() - dt.timedelta(days=1)).isoformat(),
                          subscription_expires_at=(auth.now_ist() + dt.timedelta(days=10)).isoformat())
        _login_session(client, uid)
        resp = client.get("/")
        assert resp.status_code == 200

    def test_suspended_user_cannot_login(self, client):
        _seed_user(app.DB_PATH, email="suspended@test.com", is_suspended=1)
        token = _csrf(client)
        resp = client.post("/login", data={"login_id": "suspended@test.com", "password": "Testpass123",
                                            "csrf_token": token})
        assert resp.status_code == 200   # re-rendered login page with an error, not a redirect
        assert b"suspended" in resp.data.lower()
        with client.session_transaction() as sess:
            assert "user_id" not in sess


class TestWalletLedger:
    def test_balance_math_and_clamp(self, client):
        uid = _seed_user(app.DB_PATH, email="wallet@test.com", wallet_balance=50000)
        new_balance = billing.create_wallet_transaction(uid, -1000, "admin_adjustment", note="test debit")
        assert new_balance == 49000

        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute(
            "SELECT amount, balance_after, reason FROM wallet_transactions WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (uid,),
        ).fetchone()
        conn.close()
        assert row == (-1000, 49000, "admin_adjustment")

        clamped = billing.create_wallet_transaction(uid, -999999, "admin_adjustment")
        assert clamped == 0.0

    def test_invalid_reason_rejected(self, client):
        uid = _seed_user(app.DB_PATH, email="wallet2@test.com")
        with pytest.raises(ValueError):
            billing.create_wallet_transaction(uid, 100, "not_a_real_reason")


class TestLoginRateLimit:
    def test_lockout_then_recovery(self, client):
        _seed_user(app.DB_PATH, email="lockout@test.com", password="RightPass123")
        token = _csrf(client)
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            resp = client.post("/login", data={"login_id": "lockout@test.com", "password": "WrongPass",
                                                "csrf_token": token})
            assert resp.status_code == 200

        # Even the CORRECT password is now rejected while locked out.
        resp = client.post("/login", data={"login_id": "lockout@test.com", "password": "RightPass123",
                                            "csrf_token": token})
        assert b"too many" in resp.data.lower() or b"attempts" in resp.data.lower()
        with client.session_transaction() as sess:
            assert "user_id" not in sess

        time.sleep(auth.LOGIN_LOCKOUT_SECONDS + 0.5)
        token = _csrf(client)
        resp = client.post("/login", data={"login_id": "lockout@test.com", "password": "RightPass123",
                                            "csrf_token": token})
        assert resp.status_code == 302   # lockout window passed, correct password now accepted


class TestCsrf:
    def test_rejected_without_token(self, client):
        uid = _seed_user(app.DB_PATH, username="csrfadmin", role="admin")
        _login_session(client, uid)
        resp = client.post(f"/admin/users/{uid}/wallet", data={"amount": "100"})
        assert resp.status_code == 400

    def test_accepted_with_valid_token(self, client):
        uid = _seed_user(app.DB_PATH, username="csrfadmin2", role="admin")
        _login_session(client, uid)
        token = "test-csrf-token-value"
        with client.session_transaction() as sess:
            sess["csrf_token"] = token
        resp = client.post(f"/admin/users/{uid}/wallet", data={"amount": "100", "csrf_token": token})
        assert resp.status_code == 302


class TestRouteProtectionSelfCheck:
    def test_self_check_passes_on_current_app(self):
        app._verify_all_routes_protected()   # must not raise

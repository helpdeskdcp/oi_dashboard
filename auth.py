#!/usr/bin/env python3
"""
auth.py -- Accounts, sessions, roles, CSRF, and email verification for the
oi_dashboard multi-user system (Administrator / Developer / Subscriber).

Standalone module (own DB_PATH/logger, no import of app.py) -- same
convention as nse_fetcher.py/history_engine.py, to avoid a circular import
back into app.py (app.py imports this module, not the other way around).

SECURITY MODEL (read this before touching anything below):
- Passwords: werkzeug.security (scrypt-backed), never stored/logged raw.
- Email verification tokens: only the SHA-256 hash is ever persisted. The
  raw token exists only in the outgoing email / a one-time authenticated
  admin response -- never in a DB column, never in a public API response,
  never in a log line.
- Sessions store ONLY a user id (+ a CSRF token) -- role/subscription/wallet
  are re-read from SQLite on every request (see app.py's before_request user
  loader), so an admin action (suspend, role change, wallet edit) takes
  effect on the affected user's very next request, not after they re-login.
- Login is rate-limited per (client IP, login id) to blunt brute-forcing.
"""

import os
import hmac
import secrets
import hashlib
import logging
import smtplib
import threading
import datetime as dt
from email.mime.text import MIMEText
from functools import wraps

from flask import session, g, redirect, url_for, request, abort

from werkzeug.security import generate_password_hash, check_password_hash

log = logging.getLogger("auth")

DB_PATH = os.getenv("DB_PATH", "oi_history.db")
IST_OFFSET = dt.timedelta(hours=5, minutes=30)


def now_ist():
    """Same contract as app.py's now_ist(): a NAIVE datetime already shifted
    to IST -- every timestamp column in this DB uses this, never a tz-aware
    value, to avoid naive/aware comparison bugs against existing data."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + IST_OFFSET


# ----------------------------------------------------------------------------
# Config (all overridable via .env, same os.getenv-at-import-time convention
# app.py itself uses for its own tunables)
# ----------------------------------------------------------------------------
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "15"))
EMAIL_VERIFICATION_TOKEN_TTL_HOURS = int(os.getenv("EMAIL_VERIFICATION_TOKEN_TTL_HOURS", "24"))
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.getenv("LOGIN_LOCKOUT_SECONDS", "300"))

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "")
SMTP_ENABLED = bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM_EMAIL)


# ----------------------------------------------------------------------------
# Passwords
# ----------------------------------------------------------------------------

def hash_password(raw_password: str) -> str:
    return generate_password_hash(raw_password)


def verify_password(raw_password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    return check_password_hash(password_hash, raw_password)


def validate_password_strength(raw_password: str):
    """Returns None if OK, else an error string. Minimum bar: usable without
    complexity-rule fatigue, but blocks trivially-guessable all-digit/all-letter
    passwords for an app that will hold real subscriber accounts."""
    if not raw_password or len(raw_password) < 10:
        return "Password must be at least 10 characters."
    if not any(c.isalpha() for c in raw_password) or not any(c.isdigit() for c in raw_password):
        return "Password must contain at least one letter and one digit."
    return None


# ----------------------------------------------------------------------------
# Verification tokens -- raw token is NEVER persisted, only its hash
# ----------------------------------------------------------------------------

def generate_verification_token():
    """Returns (raw_token, sha256_hex). Only the hash is ever stored in the
    DB -- a leaked DB can never be used to replay/forge a verification link."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def token_expiry():
    return now_ist() + dt.timedelta(hours=EMAIL_VERIFICATION_TOKEN_TTL_HOURS)


# ----------------------------------------------------------------------------
# Email -- mirrors app.py's send_telegram(): config-from-env, silent no-op if
# unconfigured, broad try/except + log.warning, short timeout, never raises.
# UNLIKE send_telegram, the caller DOES need the boolean result here (the
# registration flow behaves differently depending on whether the mail could
# even be attempted -- though the user-facing message never changes, since we
# never want to reveal delivery status to an unauthenticated caller).
# ----------------------------------------------------------------------------

def send_email(to_email: str, subject: str, body_text: str) -> bool:
    if not SMTP_ENABLED:
        log.warning(f"SMTP not configured -- cannot email {to_email} ('{subject}'). "
                    f"Use the admin panel's 'Resend verification' action to get the link instead.")
        return False
    try:
        msg = MIMEText(body_text)
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM_EMAIL
        msg["To"] = to_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, [to_email], msg.as_string())
        return True
    except Exception as e:
        log.warning(f"send_email failed to {to_email}: {e}")
        return False


def verification_email_body(raw_token: str, base_url: str) -> str:
    link = f"{base_url.rstrip('/')}/verify-email/{raw_token}"
    return (
        "Welcome! Please verify your email to activate your account and start "
        f"your {TRIAL_DAYS}-day free trial.\n\n"
        f"Verify here: {link}\n\n"
        f"This link expires in {EMAIL_VERIFICATION_TOKEN_TTL_HOURS} hours.\n\n"
        "If you didn't request this, you can ignore this email."
    )


# ----------------------------------------------------------------------------
# CSRF -- dependency-free, session-bound token
# ----------------------------------------------------------------------------

def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def validate_csrf_token(submitted):
    real = session.get("csrf_token")
    return bool(real) and bool(submitted) and hmac.compare_digest(real, submitted)


def csrf_guard():
    """Call from app.py's before_request for state-changing methods. Checks
    the form field first, then the X-CSRFToken header (for fetch()-based
    POSTs), aborts 400 on mismatch/missing."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    submitted = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
    if not submitted and request.is_json:
        submitted = (request.get_json(silent=True) or {}).get("csrf_token")
    if not validate_csrf_token(submitted):
        abort(400, description="Invalid or missing CSRF token.")


# ----------------------------------------------------------------------------
# Login brute-force lockout -- in-memory, safe because this app runs as a
# single process (socketio.run, threading async_mode -- confirmed, not
# multi-worker gunicorn). Keyed by (ip, login_id) so one attacker can't lock
# out a legitimate user logging in from elsewhere.
# ----------------------------------------------------------------------------
_login_failures = {}
_login_failures_lock = threading.Lock()


def _lockout_key(ip, login_id):
    return (ip or "unknown", (login_id or "").strip().lower())


def is_login_locked(ip, login_id):
    """Returns (locked: bool, seconds_remaining: int)."""
    key = _lockout_key(ip, login_id)
    now = now_ist().timestamp()
    with _login_failures_lock:
        attempts = [t for t in _login_failures.get(key, []) if now - t < LOGIN_LOCKOUT_SECONDS]
        _login_failures[key] = attempts
        if len(attempts) >= LOGIN_MAX_ATTEMPTS:
            oldest_relevant = attempts[-LOGIN_MAX_ATTEMPTS]
            remaining = int(LOGIN_LOCKOUT_SECONDS - (now - oldest_relevant))
            return True, max(remaining, 1)
    return False, 0


def record_login_failure(ip, login_id):
    key = _lockout_key(ip, login_id)
    with _login_failures_lock:
        _login_failures.setdefault(key, []).append(now_ist().timestamp())


def reset_login_failures(ip, login_id):
    key = _lockout_key(ip, login_id)
    with _login_failures_lock:
        _login_failures.pop(key, None)


# ----------------------------------------------------------------------------
# Role / session decorators. Each tags the wrapped view with
# __auth_protected__ = True so app.py's startup self-check can verify every
# route genuinely has an access-control decorator applied (fail-closed: an
# undecorated route makes the app refuse to start, not silently serve).
# ----------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if getattr(g, "user", None) is None:
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    wrapper.__auth_protected__ = True
    return wrapper


def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if getattr(g, "user", None) is None:
                return redirect(url_for("login_page", next=request.path))
            if g.user["role"] not in roles:
                abort(403)
            return f(*args, **kwargs)
        wrapper.__auth_protected__ = True
        return wrapper
    return decorator


def subscription_required(f):
    """Implies login_required. No-op for admin/developer (staff, not paying
    customers -- never subject to trial/subscription expiry). For a
    subscriber, blocks access once BOTH the trial and any paid subscription
    have lapsed."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = getattr(g, "user", None)
        if u is None:
            return redirect(url_for("login_page", next=request.path))
        if u["role"] in ("admin", "developer"):
            return f(*args, **kwargs)
        if not subscription_is_active(u):
            return redirect(url_for("access_restricted_page", reason="expired"))
        return f(*args, **kwargs)
    wrapper.__auth_protected__ = True
    return wrapper


def subscription_is_active(user_row) -> bool:
    """user_row: a sqlite3.Row (or dict) from the users table. Active if
    either the trial window or a paid subscription window hasn't lapsed yet."""
    now = now_ist()
    trial_ends = _parse_ts(user_row["trial_ends_at"])
    if trial_ends and now <= trial_ends:
        return True
    sub_ends = _parse_ts(user_row["subscription_expires_at"])
    if sub_ends and now <= sub_ends:
        return True
    return False


def _parse_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

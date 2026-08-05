#!/usr/bin/env python3
"""
billing.py -- Paper-trading wallet ledger for the oi_dashboard multi-user
system. Standalone module (own DB_PATH/logger, no import of app.py) -- same
convention as auth.py/nse_fetcher.py/history_engine.py.

Phase 1 scope: admin-driven top-ups/adjustments only (create_wallet_transaction
is called from app.py's admin routes). Phase 2 will call the same function
with reason='trade_pnl' once trades are wired to settle against a user's
wallet -- that's the intended extension point, not a redesign.
"""

import os
import sqlite3
import logging
import datetime as dt

log = logging.getLogger("billing")

DB_PATH = os.getenv("DB_PATH", "oi_history.db")
IST_OFFSET = dt.timedelta(hours=5, minutes=30)

DEFAULT_WALLET_BALANCE = float(os.getenv("DEFAULT_WALLET_BALANCE", "50000"))
WALLET_LOW_BALANCE_THRESHOLD = float(os.getenv("WALLET_LOW_BALANCE_THRESHOLD", "500"))

VALID_REASONS = {"admin_topup", "admin_adjustment", "trial_grant", "trade_pnl", "trade_entry"}


def now_ist():
    """Same contract as app.py's/auth.py's now_ist() -- naive datetime already
    shifted to IST, matching every other timestamp column in this DB."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + IST_OFFSET


def create_wallet_transaction(user_id: int, amount: float, reason: str,
                               note: str = None, created_by_user_id: int = None) -> float:
    """Atomic ledger mutation: inserts a wallet_transactions row and updates
    users.wallet_balance in the SAME connection/transaction. amount positive
    = credit, negative = debit. Balance is clamped at 0 minimum -- Phase 1 has
    no real debits yet (admin-only top-ups/adjustments), so a negative virtual
    balance has no meaning; Phase 2's trade-settlement path inherits the same
    clamp (a subscriber's wallet can go to exactly 0 and no trading is
    possible below that, never negative).

    Returns the new balance. Raises ValueError for an unknown user_id or an
    invalid `reason` (fail loudly here -- a silently-mislabeled ledger entry
    is a real-money-adjacent bug waiting to happen later).
    """
    if reason not in VALID_REASONS:
        raise ValueError(f"Invalid wallet transaction reason: {reason!r} (expected one of {VALID_REASONS})")

    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError(f"No such user_id={user_id}")
        new_balance = max(0.0, round(row[0] + amount, 2))
        now_str = now_ist().isoformat()
        conn.execute("UPDATE users SET wallet_balance=?, updated_at=? WHERE id=?",
                     (new_balance, now_str, user_id))
        conn.execute(
            """INSERT INTO wallet_transactions (user_id, amount, balance_after, reason, note,
                                                  created_by_user_id, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (user_id, amount, new_balance, reason, note, created_by_user_id, now_str),
        )
        conn.commit()
        log.info(f"Wallet tx: user_id={user_id} amount={amount:+.2f} reason={reason} "
                 f"-> balance={new_balance:.2f}" + (f" ({note})" if note else ""))
        return new_balance
    finally:
        conn.close()


def debit_if_sufficient(user_id: int, amount: float, reason: str, note: str = None):
    """Atomic check-and-debit: verifies wallet_balance >= amount and debits in
    ONE transaction, using BEGIN IMMEDIATE to acquire SQLite's write lock at
    the START of the transaction (before the read) -- a concurrent caller for
    the SAME user_id genuinely blocks/serializes on that lock instead of both
    reading the same pre-debit balance. create_wallet_transaction() above is
    NOT safe for this: its plain SELECT-then-UPDATE has a window where two
    concurrent calls can both read the same balance and both "succeed,"
    silently overdrawing the wallet (its clamp-at-0 floor makes that failure
    silent rather than a visible error). That's fine for Phase 1's slow,
    one-at-a-time admin actions; NOT fine as the basis for a fast,
    user-triggered "can I afford this trade" check -- which is what this
    function is for.

    amount is POSITIVE (the magnitude to debit). Returns the new balance on
    success, or None if the balance was insufficient (no partial/clamped
    debit ever happens -- unlike create_wallet_transaction, this can only
    ever move the FULL amount or nothing at all). Raises ValueError for an
    unknown user_id or invalid reason, same as create_wallet_transaction.
    """
    if reason not in VALID_REASONS:
        raise ValueError(f"Invalid wallet transaction reason: {reason!r} (expected one of {VALID_REASONS})")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            conn.rollback()
            raise ValueError(f"No such user_id={user_id}")
        if row[0] < amount:
            conn.rollback()
            return None
        new_balance = round(row[0] - amount, 2)
        now_str = now_ist().isoformat()
        conn.execute("UPDATE users SET wallet_balance=?, updated_at=? WHERE id=?",
                     (new_balance, now_str, user_id))
        conn.execute(
            """INSERT INTO wallet_transactions (user_id, amount, balance_after, reason, note,
                                                  created_by_user_id, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (user_id, -amount, new_balance, reason, note, None, now_str),
        )
        conn.commit()
        log.info(f"Wallet debit: user_id={user_id} amount=-{amount:.2f} reason={reason} "
                 f"-> balance={new_balance:.2f}" + (f" ({note})" if note else ""))
        return new_balance
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_wallet_history(user_id: int, limit: int = 50):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM wallet_transactions WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

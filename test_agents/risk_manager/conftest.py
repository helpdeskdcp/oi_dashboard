"""
test_agents/risk_manager/conftest.py -- a minimal, real-shaped SQLite
schema for paper_orders/paper_trades/scalp_paper_trades/v3_paper_trades/
users/cycles/strikes (the tables agents/risk_manager/data_access.py
reads from) -- built directly here, NEVER by importing app.py (a ~7000-
line Flask app with real broker-session machinery; importing it in a
test process is exactly the kind of thing that risks tripping a real
Angel One login, a documented landmine in this repo). Column lists match
what a prior investigation confirmed directly from app.py's own
CREATE TABLE statements -- only the columns agents/risk_manager actually
reads are included, since this is a read-only consumer, not a schema
owner.
"""
import sqlite3

import pytest

from agents.risk_manager import data_access, portfolio_monitor


@pytest.fixture()
def paper_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "paper.db")
    monkeypatch.setattr(data_access, "DB_PATH", db_path)
    monkeypatch.setattr(portfolio_monitor.data_access, "DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, wallet_balance REAL DEFAULT 50000
        );

        CREATE TABLE paper_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, symbol TEXT, strike REAL,
            direction TEXT, entry_price REAL, target_price REAL, sl_price REAL, qty INTEGER DEFAULT 1,
            entry_time TEXT, entry_ts REAL, exit_price REAL, exit_time TEXT, exit_reason TEXT,
            points REAL, status TEXT DEFAULT 'OPEN'
        );

        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, strike REAL, direction TEXT,
            entry_price REAL, target_price REAL, sl_price REAL, entry_time TEXT, entry_ts REAL,
            exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL, status TEXT DEFAULT 'OPEN'
        );

        CREATE TABLE scalp_paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, strike REAL, direction TEXT,
            entry_price REAL, target_price REAL, sl_price REAL, entry_time TEXT, entry_ts REAL,
            exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL, status TEXT DEFAULT 'OPEN'
        );

        CREATE TABLE v3_paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, strike REAL, direction TEXT,
            entry_price REAL, target_price REAL, sl_price REAL, entry_time TEXT, entry_ts REAL,
            exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL, status TEXT DEFAULT 'OPEN'
        );

        CREATE TABLE cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, ts TEXT
        );

        CREATE TABLE strikes (
            cycle_id INTEGER, strike REAL,
            ce_delta REAL, ce_gamma REAL, ce_theta REAL, ce_vega REAL, ce_iv REAL,
            pe_delta REAL, pe_gamma REAL, pe_theta REAL, pe_vega REAL, pe_iv REAL
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def insert_user(db_path, user_id, wallet_balance=50000.0):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO users (id, wallet_balance) VALUES (?, ?)", (user_id, wallet_balance))
    conn.commit()
    conn.close()


def insert_paper_order(db_path, **kwargs):
    defaults = {
        "user_id": 1, "symbol": "NIFTY", "strike": 22000, "direction": "CE", "entry_price": 100.0,
        "target_price": None, "sl_price": None, "qty": 50, "entry_time": "2026-05-04T09:20:00",
        "entry_ts": 0.0, "exit_price": None, "exit_time": None, "exit_reason": None, "points": None,
        "status": "OPEN",
    }
    defaults.update(kwargs)
    conn = sqlite3.connect(db_path)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" for _ in defaults)
    conn.execute(f"INSERT INTO paper_orders ({cols}) VALUES ({placeholders})", tuple(defaults.values()))
    conn.commit()
    conn.close()


def insert_engine_trade(db_path, table, **kwargs):
    defaults = {
        "symbol": "NIFTY", "strike": 22000, "direction": "CE", "entry_price": 100.0,
        "target_price": None, "sl_price": None, "entry_time": "2026-05-04T09:20:00", "entry_ts": 0.0,
        "exit_price": None, "exit_time": None, "exit_reason": None, "points": None, "status": "OPEN",
    }
    defaults.update(kwargs)
    conn = sqlite3.connect(db_path)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" for _ in defaults)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", tuple(defaults.values()))
    conn.commit()
    conn.close()


def insert_strikes(db_path, symbol, strike, *, ts="2026-05-04T09:20:00", **greeks):
    conn = sqlite3.connect(db_path)
    cur = conn.execute("INSERT INTO cycles (symbol, ts) VALUES (?, ?)", (symbol, ts))
    cycle_id = cur.lastrowid
    row = {
        "cycle_id": cycle_id, "strike": strike,
        "ce_delta": None, "ce_gamma": None, "ce_theta": None, "ce_vega": None, "ce_iv": None,
        "pe_delta": None, "pe_gamma": None, "pe_theta": None, "pe_vega": None, "pe_iv": None,
    }
    row.update(greeks)
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO strikes ({cols}) VALUES ({placeholders})", tuple(row.values()))
    conn.commit()
    conn.close()

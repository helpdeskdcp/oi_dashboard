"""
test_agents/trading_intelligence/ -- Milestone 10 (BATI Trading
Intelligence Platform) test suite. Builds a real-shaped schema for
cycles/strikes/market_structure_snapshots directly here (never by
importing app.py -- a ~7000-line Flask app with real broker-session
machinery; importing it in a test process risks exactly the kind of
thing this project's own history already had happen once, per the
/live-positions landmine note) -- same convention
test_agents/risk_manager/conftest.py already established for
paper_orders/paper_trades/etc.

Column list matches app.py's own CREATE TABLE statements exactly
(confirmed via direct inspection, not guessed).
"""
import sqlite3

import pytest

from agents import audit_log, event_bus
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import data_access, ti_store


@pytest.fixture()
def ti_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ti_test.db")
    monkeypatch.setattr(data_access, "DB_PATH", db_path)
    monkeypatch.setattr(ti_store, "DB_PATH", db_path)
    monkeypatch.setattr(audit_log, "DB_PATH", db_path)
    monkeypatch.setattr(event_bus, "DB_PATH", db_path)
    monkeypatch.setattr(sysadmin_store, "DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, ts TEXT, date TEXT, time TEXT,
            underlying_ltp REAL, atm REAL, pcr REAL, max_pain REAL, bias TEXT, note TEXT,
            signal_action TEXT, signal_strike REAL, signal_direction TEXT, signal_entry REAL,
            signal_target REAL, signal_sl REAL, signal_confidence INTEGER, signal_tradeable INTEGER
        );
        CREATE TABLE strikes (
            cycle_id INTEGER, strike REAL,
            ce_oi INTEGER, ce_oi_chg INTEGER, ce_vol INTEGER, ce_ltp REAL, ce_chg_pct REAL, ce_signal TEXT, ce_iv REAL,
            ce_delta REAL, ce_gamma REAL, ce_theta REAL, ce_vega REAL,
            pe_oi INTEGER, pe_oi_chg INTEGER, pe_vol INTEGER, pe_ltp REAL, pe_chg_pct REAL, pe_signal TEXT, pe_iv REAL,
            pe_delta REAL, pe_gamma REAL, pe_theta REAL, pe_vega REAL
        );
        CREATE TABLE market_structure_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, date TEXT, time TEXT, ts TEXT,
            atr_14 REAL, adx REAL, regime TEXT, pdh REAL, pdl REAL, pdc REAL, vwap REAL,
            swing_high REAL, swing_low REAL,
            mother_candle_json TEXT, liquidity_sweep_json TEXT, custom_levels_json TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    audit_log.init_db()
    event_bus.init_db()
    sysadmin_store.init_db()
    ti_store.init_db()
    return db_path


def insert_cycle(db_path, *, symbol="NIFTY", ts="2026-08-06T10:00:00", underlying_ltp=24505.0,
                  atm=24500.0, pcr=1.1, max_pain=24450.0, bias=None) -> int:
    conn = sqlite3.connect(db_path)
    date, time = ts.split("T")
    cur = conn.execute(
        "INSERT INTO cycles (symbol, ts, date, time, underlying_ltp, atm, pcr, max_pain, bias) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, ts, date, time, underlying_ltp, atm, pcr, max_pain, bias),
    )
    conn.commit()
    cycle_id = cur.lastrowid
    conn.close()
    return cycle_id


def insert_strike(db_path, cycle_id, strike, **kwargs):
    defaults = {
        "ce_oi": 50000, "ce_oi_chg": 500, "ce_vol": 15000, "ce_ltp": 100.0, "ce_chg_pct": 1.0,
        "ce_signal": "Neutral", "ce_iv": 15.0,
        "pe_oi": 60000, "pe_oi_chg": 500, "pe_vol": 8000, "pe_ltp": 80.0, "pe_chg_pct": 0.5,
        "pe_signal": "Neutral", "pe_iv": 15.0,
    }
    defaults.update(kwargs)
    conn = sqlite3.connect(db_path)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" for _ in defaults)
    conn.execute(f"INSERT INTO strikes (cycle_id, strike, {cols}) VALUES (?, ?, {placeholders})",
                 (cycle_id, strike, *defaults.values()))
    conn.commit()
    conn.close()


def insert_realistic_chain(db_path, *, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0,
                            pcr=1.1, max_pain=24450.0, step=50, ts="2026-08-06T10:00:00", strikes_each_side=4):
    """A realistic 9-strike chain (ATM +/- 4, matching wanted_strikes'
    default strikes_each_side) -- large enough that oi_engine.oi_walls()'s
    top-3 logic behaves the same way it does against a real live chain,
    unlike a 2-3 strike toy chain where every row is trivially a "wall.\""""
    cycle_id = insert_cycle(db_path, symbol=symbol, ts=ts, underlying_ltp=underlying_ltp, atm=atm,
                             pcr=pcr, max_pain=max_pain)
    strikes = [atm + i * step for i in range(-strikes_each_side, strikes_each_side + 1)]
    for s in strikes:
        insert_strike(db_path, cycle_id, s)
    return cycle_id, strikes


def insert_market_structure(db_path, *, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=None, atr_14=None,
                             regime=None, **kwargs) -> int:
    """One market_structure_snapshots row -- same insert-with-sane-defaults
    style insert_cycle()/insert_strike() already establish, added for
    Milestone 11's regime_profile.py (volatility-regime history reads)
    rather than each test hand-rolling its own sqlite3.connect() insert."""
    conn = sqlite3.connect(db_path)
    date, time = ts.split("T")
    cols = {"symbol": symbol, "ts": ts, "date": date, "time": time, "adx": adx, "atr_14": atr_14,
            "regime": regime, **kwargs}
    col_names = ", ".join(cols.keys())
    placeholders = ", ".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO market_structure_snapshots ({col_names}) VALUES ({placeholders})", tuple(cols.values()),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


@pytest.fixture()
def memory_store(tmp_path):
    return SQLiteMemoryStore(db_path=str(tmp_path / "memory.db"))

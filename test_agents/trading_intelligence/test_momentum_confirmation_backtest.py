"""
test_agents/trading_intelligence/test_momentum_confirmation_backtest.py --
regression tests for momentum_confirmation_backtest.py and the
backtest.simulate_trades() extension it relies on (candles_df/
momentum_confirmation_enabled kwargs).

Momentum_exhaustion() itself is patched (its own correctness is covered by
agents.quant_researcher's own tests) -- these tests verify THIS module's
wiring: that momentum confirmation, when enabled, can genuinely change
which trades get taken (via the confidence threshold), that it's a no-op
when disabled (backward compatibility with every existing simulate_trades()
caller), and that compare_symbol() aggregates both runs correctly.
"""
import os
os.environ["SKIP_AUTOSTART"] = "1"

import sqlite3
import datetime as dt
from unittest.mock import patch

import pandas as pd
import pytest

import backtest
from agents.trading_intelligence import momentum_confirmation_backtest as mcb


@pytest.fixture()
def db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(backtest, "DB_PATH", db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, ts TEXT, date TEXT, time TEXT,
            underlying_ltp REAL, atm INTEGER, pcr REAL, max_pain INTEGER,
            bias TEXT, note TEXT,
            signal_action TEXT, signal_strike INTEGER, signal_direction TEXT,
            signal_entry REAL, signal_target REAL, signal_sl REAL, signal_confidence INTEGER,
            signal_tradeable INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE strikes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER,
            strike INTEGER, ce_oi INTEGER, ce_oi_chg INTEGER, ce_vol INTEGER, ce_ltp REAL,
            ce_chg_pct REAL, ce_signal TEXT, ce_iv REAL, ce_delta REAL, ce_gamma REAL,
            ce_theta REAL, ce_vega REAL,
            pe_oi INTEGER, pe_oi_chg INTEGER, pe_vol INTEGER, pe_ltp REAL,
            pe_chg_pct REAL, pe_signal TEXT, pe_iv REAL, pe_delta REAL, pe_gamma REAL,
            pe_theta REAL, pe_vega REAL
        )
    """)
    conn.commit()
    conn.close()
    return db_path


def _seed_cycle(db_path, *, symbol="NIFTY", ts, atm=25000, pcr=1.0, bias="BULLISH BIAS",
                 ce_ltp=100.0, pe_ltp=100.0):
    """One cycle with a plain BULLISH bias and neutral PCR/volume/signal
    fields -- generate_signal() computes confidence=50 for this (base
    only, no PCR/volume/buildup/breakout bonuses), so a momentum
    confirmation bonus (+10) is exactly what pushes it to the default
    confidence_threshold=60, and a penalty keeps it below."""
    date, time = ts.split("T")
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO cycles (symbol, ts, date, time, underlying_ltp, atm, pcr, bias, note) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (symbol, ts, date, time, atm, atm, pcr, bias, "test cycle"),
    )
    cycle_id = cur.lastrowid
    conn.execute(
        "INSERT INTO strikes (cycle_id, strike, ce_oi, ce_oi_chg, ce_vol, ce_ltp, ce_chg_pct, ce_signal, "
        "pe_oi, pe_oi_chg, pe_vol, pe_ltp, pe_chg_pct, pe_signal) "
        "VALUES (?,?,100,0,100,?,0.0,?,100,0,100,?,0.0,?)",
        (cycle_id, atm, ce_ltp, "Neutral", pe_ltp, "Neutral"),
    )
    conn.commit()
    conn.close()


def _seed_hold_cycle(db_path, *, symbol="NIFTY", ts, atm=25000, ce_ltp=100.0, pe_ltp=100.0):
    """A follow-up cycle at the SAME strike/price, far enough after entry
    to force a TIME EXIT close (MAX_HOLD_MINUTES=30) without accidentally
    hitting target/SL -- simulate_trades() only ever appends a trade to
    its returned list once it CLOSES, so a single-cycle seed opens a
    position that never lands in the result at all."""
    date, time = ts.split("T")
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO cycles (symbol, ts, date, time, underlying_ltp, atm, pcr, bias, note) "
        "VALUES (?,?,?,?,?,?,1.0,'BULLISH BIAS','hold')",
        (symbol, ts, date, time, atm, atm),
    )
    cycle_id = cur.lastrowid
    conn.execute(
        "INSERT INTO strikes (cycle_id, strike, ce_oi, ce_oi_chg, ce_vol, ce_ltp, ce_chg_pct, ce_signal, "
        "pe_oi, pe_oi_chg, pe_vol, pe_ltp, pe_chg_pct, pe_signal) "
        "VALUES (?,?,100,0,100,?,0.0,'Neutral',100,0,100,?,0.0,'Neutral')",
        (cycle_id, atm, ce_ltp, pe_ltp),
    )
    conn.commit()
    conn.close()


def _candles_df(closes):
    # Starts well before every test's cycle ts (09:20:00) -- generate_signal()'s
    # momentum block needs at least 15 PAST candles once the no-lookahead
    # slice (candles at or before the cycle's own ts) is applied.
    base = dt.datetime(2026, 8, 6, 7, 0, 0)
    return pd.DataFrame({
        "datetime": [base + dt.timedelta(minutes=3 * i) for i in range(len(closes))],
        "open": closes, "high": closes, "low": closes, "close": closes,
    })


class TestSummarize:
    def test_counts_wins_losses_and_net_points(self):
        trades = [
            {"exit_reason": "TARGET HIT", "points": 50.0},
            {"exit_reason": "STOP LOSS", "points": -20.0},
            {"exit_reason": "TIME EXIT", "points": 5.0},
        ]
        count, wins, losses, rate, net = mcb._summarize(trades)
        assert count == 3
        assert wins == 1
        assert losses == 1
        assert net == 35.0

    def test_win_rate_none_below_sample_floor(self):
        trades = [{"exit_reason": "TARGET HIT", "points": 1.0}] * 5
        _, _, _, rate, _ = mcb._summarize(trades)
        assert rate is None

    def test_win_rate_reported_at_or_above_sample_floor(self):
        trades = ([{"exit_reason": "TARGET HIT", "points": 1.0}] * 15
                   + [{"exit_reason": "STOP LOSS", "points": -1.0}] * 5)
        _, wins, losses, rate, _ = mcb._summarize(trades)
        assert wins == 15 and losses == 5
        assert rate == 0.75

    def test_empty_trades_no_crash(self):
        count, wins, losses, rate, net = mcb._summarize([])
        assert count == 0 and net == 0.0 and rate is None


class TestSimulateTradesBackwardCompatibility:
    def test_default_kwargs_behave_identically_to_before(self, db):
        _seed_cycle(db, ts="2026-08-06T09:20:00")
        trades_default, count_default = backtest.simulate_trades("NIFTY", "2026-08-06", "2026-08-06", 1, 5, 60)
        trades_explicit_off, count_explicit = backtest.simulate_trades(
            "NIFTY", "2026-08-06", "2026-08-06", 1, 5, 60,
            momentum_confirmation_enabled=False, candles_df=_candles_df([100.0] * 20),
        )
        assert count_default == count_explicit
        assert trades_default == trades_explicit_off   # momentum OFF must never be influenced by candles_df

    def test_confidence_50_cycle_is_not_tradeable_without_momentum(self, db):
        _seed_cycle(db, ts="2026-08-06T09:20:00")
        trades, _ = backtest.simulate_trades("NIFTY", "2026-08-06", "2026-08-06", 1, 5, 60)
        assert trades == []   # base confidence 50 < default threshold 60 -- no trade taken


class TestMomentumFlagChangesWhichTradesAreTaken:
    def test_positive_momentum_confirmation_makes_a_marginal_signal_tradeable(self, db):
        _seed_cycle(db, ts="2026-08-06T09:20:00")
        _seed_hold_cycle(db, ts="2026-08-06T09:52:00")   # +32min -- forces a TIME EXIT close
        with patch("agents.quant_researcher.features.momentum_exhaustion",
                   return_value=pd.Series([0.5] * 20)):
            trades, _ = backtest.simulate_trades(
                "NIFTY", "2026-08-06", "2026-08-06", 1, 5, 60,
                momentum_confirmation_enabled=True, candles_df=_candles_df([100.0] * 20),
            )
        assert len(trades) == 1   # +10 bonus pushes confidence 50 -> 60, now tradeable
        assert trades[0]["strike"] == 25000
        assert trades[0]["direction"] == "CE"
        assert trades[0]["exit_reason"] == "TIME EXIT"

    def test_negative_momentum_keeps_it_below_threshold(self, db):
        _seed_cycle(db, ts="2026-08-06T09:20:00")
        with patch("agents.quant_researcher.features.momentum_exhaustion",
                   return_value=pd.Series([-0.5] * 20)):
            trades, _ = backtest.simulate_trades(
                "NIFTY", "2026-08-06", "2026-08-06", 1, 5, 60,
                momentum_confirmation_enabled=True, candles_df=_candles_df([100.0] * 20),
            )
        assert trades == []   # -10 penalty keeps confidence at 40, still below threshold

    def test_no_candles_available_degrades_to_off_behavior(self, db):
        """Fewer than 15 candles (or none at all) -- generate_signal()'s own
        guard skips the momentum block entirely, never raises."""
        _seed_cycle(db, ts="2026-08-06T09:20:00")
        trades, _ = backtest.simulate_trades(
            "NIFTY", "2026-08-06", "2026-08-06", 1, 5, 60,
            momentum_confirmation_enabled=True, candles_df=_candles_df([100.0] * 5),
        )
        assert trades == []


class TestCompareSymbol:
    def test_reports_both_off_and_on_results(self, db):
        _seed_cycle(db, ts="2026-08-06T09:20:00")
        _seed_hold_cycle(db, ts="2026-08-06T09:52:00")   # +32min -- forces a TIME EXIT close
        with patch("agents.quant_researcher.features.momentum_exhaustion",
                   return_value=pd.Series([0.5] * 20)):
            result = mcb.compare_symbol("NIFTY", "2026-08-06", "2026-08-06", persistence_cycles=1,
                                         candles_df=_candles_df([100.0] * 20))
        assert result.symbol == "NIFTY"
        assert result.off_trade_count == 0    # confidence 50 < 60, no momentum help
        assert result.on_trade_count == 1     # momentum bonus made it tradeable

    def test_never_writes_to_the_database(self, db):
        """Pure read/replay -- calling compare_symbol() must never modify
        the cycles/strikes tables it reads from."""
        _seed_cycle(db, ts="2026-08-06T09:20:00")
        conn = sqlite3.connect(db)
        before = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        conn.close()

        mcb.compare_symbol("NIFTY", "2026-08-06", "2026-08-06", candles_df=_candles_df([100.0] * 20))

        conn = sqlite3.connect(db)
        after = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        conn.close()
        assert before == after

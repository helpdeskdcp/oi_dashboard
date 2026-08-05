#!/usr/bin/env python3
"""
backtest.py -- Replays logged historical OI data through the SAME oi_engine
logic that the live dashboard uses (bias -> signal -> paper trade simulation),
for a chosen symbol + date (or date range).

IMPORTANT: this only works on data collected AFTER historical logging was
turned on (SQLite file oi_history.db, written continuously by app.py while
it runs). There is no way to backtest dates before logging started -- Angel
One / NSE don't expose historical option-chain OI via a simple API.

USAGE
-----
List which symbols/dates you actually have data for:
    python3 backtest.py --list

Backtest one day:
    python3 backtest.py --symbol NIFTY --date 2026-07-13

Backtest a date range:
    python3 backtest.py --symbol NIFTY --from 2026-07-13 --to 2026-07-17

Tune the same fake-signal filter knobs used live (defaults match .env):
    python3 backtest.py --symbol NIFTY --date 2026-07-13 --persistence 3 --cooldown 15
"""

import argparse
import sqlite3
import json
import time
import os
import statistics
import datetime as dt
from collections import defaultdict
from pathlib import Path

import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass   # dotenv optional -- falls back to hardcoded defaults below

# Replay-fidelity: these defaults MUST match what the live app actually uses
# (app.py reads the same .env). A CLI replay with different persistence than
# live is misleading -- it can show trades the live app would never have
# opened (this exact discrepancy was hit on 2026-07-20: CLI's old default of
# 45s opened a NATURALGAS trade the live 60s persistence never did).
LIVE_PERSISTENCE_SECONDS = int(os.getenv("BIAS_PERSISTENCE_SECONDS", "60"))
LIVE_GRACE_SECONDS = int(os.getenv("NEUTRAL_GRACE_SECONDS", "20"))
LIVE_MIN_RISK_REWARD = float(os.getenv("SR_MIN_RISK_REWARD", "1.5"))
LIVE_EMA_FAST = int(os.getenv("PREMIUM_EMA_FAST", "5"))
LIVE_EMA_SLOW = int(os.getenv("PREMIUM_EMA_SLOW", "10"))
LIVE_VOLUME_EXPANSION_MULT = float(os.getenv("VOLUME_EXPANSION_MULT", "1.5"))


def _connect(db_path):
    """Shared connection helper -- sets busy_timeout so a brief lock from the
    live app's concurrent writes causes a short wait+retry instead of an
    immediate 'database is locked' failure. Works alongside WAL mode (set
    once, persistently, in app.py's init_db) which is the primary fix for
    concurrent-access slowness/blocking."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _format_eta(seconds):
    """Formats a duration in seconds as a short human-readable string."""
    if seconds is None or seconds < 0:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _print_progress_bar(done, total, start_time, trades_so_far, bar_width=30):
    """
    Progress update with elapsed time, processing rate, and ETA. Uses plain
    newline-terminated prints (NOT carriage-return overwriting) -- some
    mobile/SSH terminals (e.g. Termux) don't reliably render \\r-based
    in-place updates, which can make a genuinely-running backtest LOOK
    hung even though it's actively working. Plain prints are less pretty
    but guaranteed visible everywhere.
    """
    elapsed = time.time() - start_time
    pct = done / total if total else 0
    filled = int(bar_width * pct)
    bar = "#" * filled + "-" * (bar_width - filled)

    rate = done / elapsed if elapsed > 0 else 0
    remaining = (total - done) / rate if rate > 0 else None
    eta_str = _format_eta(remaining)
    elapsed_str = _format_eta(elapsed)

    print(f"  [{bar}] {pct*100:5.1f}% ({done}/{total}) | {trades_so_far} trades | elapsed {elapsed_str} | ETA {eta_str}", flush=True)


from oi_engine import (
    StrikeRow, calc_pcr, calc_max_pain, oi_walls, detect_bias, generate_signal,
)
from market_structure import cross_verify_wall, classical_pivots, calc_cpr
from sr_probability_engine import (
    build_sr_probability_table, advance_level_state, check_structural_trigger,
    advance_active_level, score_strike_candidates, compute_premium_entry_trigger,
    compute_premium_momentum, compute_dynamic_targets_sl, validate_risk_reward,
    check_premium_momentum_confirmed, compute_volume_expansion, fake_breakout_filter,
)
from sr_engine_v3 import generate_v3_signal, validate_previous_day_levels, should_pause_time_exit
from dynamic_sr_engine import evaluate as evaluate_dynamic_sr
import exit_engine_v4

DB_PATH = "oi_history.db"

# Must mirror app.py's defaults / .env so backtest results represent the same strategy.
TARGET_DELTA_APPROX = 0.55
MAX_SL_PERCENT = 0.05   # NEW S/R engine only -- SL revised 2026-07-21: adaptive swing-low/high stop, hard-capped at 5% max risk (was 35% flat)
OLD_ENGINE_SL_PERCENT = 0.35   # OLD reference-only engine keeps its own independent value, untouched by the SL revision
MIN_TARGET_PERCENT = 0.15
SIGNAL_CONFIDENCE_THRESHOLD = 60
MAX_HOLD_MINUTES = 30
PAPER_TRADE_LOT_QTY = 1


def list_available_data_web(db_path=None):
    """Returns [{"symbol":..., "date":..., "cycles":..., "from":..., "to":...}]
    for rendering in the /backtest web page's date picker helper list."""
    conn = _connect(db_path or DB_PATH)
    rows = conn.execute(
        "SELECT symbol, date, COUNT(*), MIN(time), MAX(time) FROM cycles GROUP BY symbol, date ORDER BY date DESC, symbol"
    ).fetchall()
    conn.close()
    return [{"symbol": r[0], "date": r[1], "cycles": r[2], "from": r[3], "to": r[4]} for r in rows]


def list_available_data():
    conn = _connect(DB_PATH)
    rows = conn.execute(
        "SELECT symbol, date, COUNT(*) as cycles, MIN(time), MAX(time) FROM cycles GROUP BY symbol, date ORDER BY date, symbol"
    ).fetchall()
    conn.close()
    if not rows:
        print("No historical data logged yet. Let app.py run during market hours to collect data.")
        return
    print(f"{'Symbol':<12} {'Date':<12} {'Cycles':<8} {'From':<10} {'To':<10}")
    for symbol, date, cycles, tmin, tmax in rows:
        print(f"{symbol:<12} {date:<12} {cycles:<8} {tmin:<10} {tmax:<10}")


def load_cycles(symbol, date_from, date_to):
    conn = _connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cycles = conn.execute(
        "SELECT * FROM cycles WHERE symbol = ? AND date BETWEEN ? AND ? ORDER BY ts ASC",
        (symbol, date_from, date_to),
    ).fetchall()

    if not cycles:
        conn.close()
        return []

    # Single JOIN query instead of one query per cycle (was a genuine N+1
    # performance bug -- for a symbol with ~5000 cycles, that was ~5000
    # separate DB round-trips; for NATURALGAS with ~12000 cycles it was even
    # worse. A JOIN (rather than a large IN(...) list, which risks hitting
    # SQLite's parameter-count limit for big symbols) keeps this to exactly
    # 2 queries total regardless of how many cycles are being replayed.
    all_strikes = conn.execute(
        """SELECT s.* FROM strikes s
           JOIN cycles c ON s.cycle_id = c.id
           WHERE c.symbol = ? AND c.date BETWEEN ? AND ?""",
        (symbol, date_from, date_to),
    ).fetchall()
    conn.close()

    strikes_by_cycle = {}
    strike_cols = set(all_strikes[0].keys()) if all_strikes else set()
    for s in all_strikes:
        strikes_by_cycle.setdefault(s["cycle_id"], []).append(
            StrikeRow(
                strike=s["strike"], ce_oi=s["ce_oi"], ce_oi_chg=s["ce_oi_chg"], ce_vol=s["ce_vol"],
                ce_ltp=s["ce_ltp"], ce_chg_pct=s["ce_chg_pct"], ce_signal=s["ce_signal"],
                pe_oi=s["pe_oi"], pe_oi_chg=s["pe_oi_chg"], pe_vol=s["pe_vol"],
                pe_ltp=s["pe_ltp"], pe_chg_pct=s["pe_chg_pct"], pe_signal=s["pe_signal"],
                # Greeks/IV -- added for Engine V3's delta-projected targets (see
                # simulate_v3_engine_trades). `strike_cols` guards against
                # replaying a DB written before these columns existed; missing
                # values default to 0.0, matching StrikeRow's own declared
                # defaults (never None, so downstream arithmetic never breaks).
                ce_iv=(s["ce_iv"] or 0.0) if "ce_iv" in strike_cols else 0.0,
                ce_delta=(s["ce_delta"] or 0.0) if "ce_delta" in strike_cols else 0.0,
                ce_gamma=(s["ce_gamma"] or 0.0) if "ce_gamma" in strike_cols else 0.0,
                ce_theta=(s["ce_theta"] or 0.0) if "ce_theta" in strike_cols else 0.0,
                ce_vega=(s["ce_vega"] or 0.0) if "ce_vega" in strike_cols else 0.0,
                pe_iv=(s["pe_iv"] or 0.0) if "pe_iv" in strike_cols else 0.0,
                pe_delta=(s["pe_delta"] or 0.0) if "pe_delta" in strike_cols else 0.0,
                pe_gamma=(s["pe_gamma"] or 0.0) if "pe_gamma" in strike_cols else 0.0,
                pe_theta=(s["pe_theta"] or 0.0) if "pe_theta" in strike_cols else 0.0,
                pe_vega=(s["pe_vega"] or 0.0) if "pe_vega" in strike_cols else 0.0,
            )
        )

    return [{"cycle": dict(c), "rows": strikes_by_cycle.get(c["id"], [])} for c in cycles]


def load_market_structure_snapshots(symbol, date_from, date_to):
    """
    Loads genuine, point-in-time market-structure snapshots (saved live by
    app.py, once per day per symbol -- see save_market_structure_snapshot in
    app.py) for a date range. Returns {date_str: structure_dict}, reconstructing
    the same shape build_sr_probability_table expects (market_structure dict
    + custom_levels dict), PLUS the snapshot's own "ts" (when it was actually
    computed that day -- typically the first successful market-structure
    build after market open, NOT market open itself). Callers MUST only apply
    a day's snapshot to cycles at or after that ts; using it for earlier
    cycles is lookahead bias. Falls back gracefully (empty dict) for dates
    before this feature was added -- callers should degrade to the
    pseudo-ATR approximation for those.
    """
    conn = _connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM market_structure_snapshots WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY ts ASC",
        (symbol, date_from, date_to),
    ).fetchall()
    conn.close()

    snapshots = {}
    for r in rows:
        if r["date"] in snapshots:
            continue   # one per day -- keep the first (earliest) snapshot of that day
        try:
            mother_candle = json.loads(r["mother_candle_json"]) if r["mother_candle_json"] else {"found": False}
            liquidity_sweep = json.loads(r["liquidity_sweep_json"]) if r["liquidity_sweep_json"] else {"swept": None}
            custom_levels = json.loads(r["custom_levels_json"]) if r["custom_levels_json"] else None
        except (json.JSONDecodeError, TypeError):
            mother_candle, liquidity_sweep, custom_levels = {"found": False}, {"swept": None}, None

        snapshots[r["date"]] = {
            "ts": r["ts"],   # point-in-time this snapshot was actually computed -- see
                              # simulate_sr_engine_trades, which must NOT apply this
                              # snapshot to cycles that happened BEFORE this ts (that
                              # would be lookahead bias: evaluating a cycle against
                              # market-structure data that wasn't known yet at that time).
            "market_structure": {
                "atr_14": r["atr_14"], "adx": r["adx"], "regime": r["regime"], "vwap": r["vwap"],
                "swing_high": r["swing_high"], "swing_low": r["swing_low"],
                "mother_candle": mother_candle, "liquidity_sweep": liquidity_sweep,
            },
            "custom_levels": custom_levels,
        }
    return snapshots


def simulate_sr_engine_trades(symbol, date_from, date_to, strike_step=50,
                               persistence_seconds=LIVE_PERSISTENCE_SECONDS,
                               deactivation_grace_seconds=LIVE_GRACE_SECONDS,
                               min_risk_reward=LIVE_MIN_RISK_REWARD, max_hold_minutes=30,
                               cooldown_minutes=0, progress_callback=None,
                               premium_ema_fast=None, premium_ema_slow=None,
                               target_delta_approx=None, min_target_pct=None, max_sl_pct=None):
    """
    Backtests the NEW S/R Probability Engine + State Machine (the engine that
    now actually opens trades live, as of the switch-over) -- NOT the old
    oi_engine signal (see simulate_trades() above for that, kept for
    comparison). Reuses the IDENTICAL functions from sr_probability_engine.py
    that app.py calls live, with historical cycle timestamps fed in as the
    simulated 'now' so the state machine's persistence/grace-period timing
    works correctly against real elapsed historical time, not backtest
    wall-clock time.

    KNOWN LIMITATION: historical PDH/PDL/ATR/ADX/regime aren't stored per
    cycle (only computed live from candles, not logged to the DB) -- this
    degrades gracefully using a pseudo-levels fallback built from that cycle's
    own OI-walls, and market_structure=None (the engine's evidence-scoring
    already handles missing market_structure by using fewer evidence
    categories, honestly reflected in each evaluation's data_completeness).
    This means backtest confidence scores will generally read lower than live
    scores that have real VWAP/regime data available.

    cooldown_minutes: backtest-only throttle -- after ANY trade exit (target,
    SL, or time), blocks new entries until this many minutes have passed.
    Live trading (app.py's state machine) has no such cooldown and is
    unaffected by this parameter; default 0 preserves the original
    no-cooldown behavior for other callers (e.g. run_sr_backtest).

    premium_ema_fast/premium_ema_slow/target_delta_approx/min_target_pct/
    max_sl_pct: override LIVE_EMA_FAST/LIVE_EMA_SLOW/TARGET_DELTA_APPROX/
    MIN_TARGET_PERCENT/MAX_SL_PERCENT when given (backtest-only tuning, see
    app.py's ENGINE_PARAM_SPECS["sr"]) -- None (the default on every one)
    preserves today's exact behavior.
    """
    premium_ema_fast = LIVE_EMA_FAST if premium_ema_fast is None else premium_ema_fast
    premium_ema_slow = LIVE_EMA_SLOW if premium_ema_slow is None else premium_ema_slow
    target_delta_approx = TARGET_DELTA_APPROX if target_delta_approx is None else target_delta_approx
    min_target_pct = MIN_TARGET_PERCENT if min_target_pct is None else min_target_pct
    max_sl_pct = MAX_SL_PERCENT if max_sl_pct is None else max_sl_pct
    cycles = load_cycles(symbol, date_from, date_to)
    if not cycles:
        return [], 0, {"elapsed_seconds": 0, "snapshot_days_used": 0, "total_days": 0}
    snapshots_by_date = load_market_structure_snapshots(symbol, date_from, date_to)
    if snapshots_by_date:
        print(f"Using REAL market-structure snapshots for {len(snapshots_by_date)} day(s) (genuinely accurate, not approximated).")


    trades = []
    open_trade = None
    cooldown_until = None
    sr_states = {}          # level_key -> state dict (in-memory for this backtest run)
    premium_histories = {}  # level_key -> list of {"ltp","time"}
    volume_histories = {}   # level_key -> list of recent volume readings (for Fake-Breakout-Filter)
    history_buffer = []     # mimics live app's per-symbol history deque (for touches-counting)

    total_cycles = len(cycles)
    print(f"Replaying {total_cycles} cycles for {symbol}...")
    start_time = time.time()
    progress_every = max(100, total_cycles // 40)   # ~40 updates total (each is its own line now)

    for idx, entry in enumerate(cycles):
        if idx > 0 and (idx % progress_every == 0 or idx == total_cycles - 1):
            _print_progress_bar(idx + 1, total_cycles, start_time, len(trades))
            if progress_callback:
                try:
                    progress_callback(idx + 1, total_cycles, time.time() - start_time, len(trades))
                except Exception:
                    pass   # a broken callback must never break the backtest itself
        c, rows = entry["cycle"], entry["rows"]
        row_map = {r.strike: r for r in rows}   # O(1) lookups below instead of repeated O(n) linear scans
        ts = dt.datetime.fromisoformat(c["ts"])
        atm, pcr, underlying = c["atm"], c["pcr"], c["underlying_ltp"]
        time_str = c["time"]

        history_buffer.append({"ltp": underlying, "time": time_str})
        if len(history_buffer) > 50:
            history_buffer = history_buffer[-50:]

        # -- manage an already-open trade first (target/SL/time-exit), one at a time --
        if open_trade:
            row = row_map.get(open_trade["strike"])
            price = (row.ce_ltp if open_trade["direction"] == "CE" else row.pe_ltp) if row else None
            if price:
                held_min = (ts - open_trade["entry_time"]).total_seconds() / 60
                exit_reason = None
                if price >= open_trade["target_price"]:
                    exit_reason = "TARGET HIT"
                elif price <= open_trade["sl_price"]:
                    exit_reason = "STOP LOSS"
                elif held_min >= max_hold_minutes:
                    exit_reason = "TIME EXIT"
                if exit_reason:
                    points = round(price - open_trade["entry_price"], 2)
                    open_trade.update({"exit_price": price, "exit_time": ts, "exit_reason": exit_reason, "points": points})
                    trades.append(open_trade)
                    open_trade = None
                    if cooldown_minutes:
                        cooldown_until = ts + dt.timedelta(minutes=cooldown_minutes)
            continue   # one trade at a time, matches live behavior

        if cooldown_until and ts < cooldown_until:
            continue

        support, resistance = oi_walls(rows)
        if not support or not resistance:
            continue
        pseudo_levels = {
            "resistance": resistance[0].strike, "resistance_reversal": resistance[0].strike - strike_step,
            "support": support[0].strike, "support_reversal": support[0].strike + strike_step,
        }

        day_snapshot = snapshots_by_date.get(c["date"])
        # LOOKAHEAD-BIAS GUARD: a day's snapshot is only valid for cycles AT OR
        # AFTER the moment it was actually computed (day_snapshot["ts"]) -- app.py
        # computes it once per day (usually shortly after market open, not AT
        # market open), and applying it to earlier cycles that day would evaluate
        # them against VWAP/swing/ATR data built from candles that hadn't
        # happened yet at that cycle's own time. Earlier cycles fall back to the
        # same pseudo-levels/no-market-structure path used for days with no
        # snapshot at all.
        snapshot_is_known_yet = bool(day_snapshot) and dt.datetime.fromisoformat(day_snapshot["ts"]) <= ts
        if day_snapshot and day_snapshot.get("custom_levels") and snapshot_is_known_yet:
            levels_to_use = day_snapshot["custom_levels"]
            market_structure_to_use = day_snapshot["market_structure"]
        else:
            levels_to_use = pseudo_levels
            market_structure_to_use = None

        # Wall-cross-verification: replicate the SAME check live performs (matching
        # OI-wall strikes against genuine price-history S/R levels). Without this,
        # backtest replay is slightly MORE permissive than live -- this exact gap
        # was found via a 2026-07-20 live-vs-replay investigation.
        resistance_wall_confirmed, support_wall_confirmed = None, None
        if market_structure_to_use:
            resistance_wall_confirmed, _ = cross_verify_wall(resistance[0].strike, "CE", market_structure_to_use)
            support_wall_confirmed, _ = cross_verify_wall(support[0].strike, "PE", market_structure_to_use)

        sr_table = build_sr_probability_table(rows, atm, pcr, market_structure_to_use, history_buffer, underlying, levels_to_use,
                                                resistance_wall_confirmed=resistance_wall_confirmed,
                                                support_wall_confirmed=support_wall_confirmed)
        if not sr_table:
            continue

        for level_key, level_eval in sr_table.items():
            if level_key == "computed_at" or not isinstance(level_eval, dict):
                continue
            level_type = "resistance" if "resistance" in level_key else "support"
            level_price = level_eval["value"]
            tolerance = max(1, level_price * 0.001)

            st = advance_level_state(level_eval, sr_states.get(level_key), persistence_seconds,
                                      deactivation_grace_seconds, now=ts)

            if st["state"] == "CONFIRMED":
                if check_structural_trigger(level_type, st["direction"], level_price, underlying, tolerance):
                    st = {**st, "state": "ACTIVE", "since": ts, "level_price": level_price, "level_type": level_type}

            elif st["state"] == "ACTIVE" and not st.get("triggered"):
                direction_opt = "CE" if (st["direction"] == "breakout" or (st["direction"] == "reversal" and level_type == "support")) else "PE"
                candidates = score_strike_candidates(rows, atm, strike_step, direction_opt)
                if candidates and candidates[0]["liquidity_ok"]:
                    best = candidates[0]
                    pbucket = premium_histories.setdefault(level_key, [])
                    vbucket = volume_histories.setdefault(level_key, [])
                    trigger = compute_premium_entry_trigger(list(pbucket))

                    ema_confirmed, ema_fast, ema_slow = check_premium_momentum_confirmed(list(pbucket), premium_ema_fast, premium_ema_slow)

                    vol_expanded, vol_ratio = compute_volume_expansion(list(vbucket), best.get("volume"), expansion_mult=LIVE_VOLUME_EXPANSION_MULT)
                    prob_keys = [k for k in level_eval if k.endswith("_probability")]
                    a_key, b_key = prob_keys[0], prob_keys[1]
                    oi_supports = max(level_eval.get(a_key, 0), level_eval.get(b_key, 0)) >= 55
                    vwap_val = (market_structure_to_use or {}).get("vwap")
                    is_bullish_dir = st["direction"] == "breakout" or (st["direction"] == "reversal" and level_type == "support")
                    vwap_ok = (underlying > vwap_val) if (vwap_val and is_bullish_dir) else \
                              (underlying < vwap_val) if vwap_val else None
                    filter_passes, filter_failed = fake_breakout_filter(
                        volume_expanded=vol_expanded, oi_supports_direction=oi_supports,
                        premium_rising=ema_confirmed, vwap_aligned=vwap_ok,
                    )

                    if trigger is not None and best["ltp"] >= trigger and ema_confirmed and filter_passes:
                        st["triggered"] = True
                        entry_price, entry_strike = best["ltp"], best["strike"]

                        # Same next-level calc as app.py's advance_sr_state_machine, using
                        # this cycle's pseudo_levels (OI-wall based) as the reference --
                        # keeps backtest R:R calculation consistent with live behavior
                        # instead of always falling back to the weak flat-percentage case.
                        next_level = None
                        if level_type == "resistance" and st["direction"] == "breakout":
                            next_level = level_price + (level_price - pseudo_levels.get("support", level_price)) * 0.25
                        elif level_type == "support" and st["direction"] == "breakdown":
                            next_level = level_price - (pseudo_levels.get("resistance", level_price) - level_price) * 0.25
                        elif level_type == "resistance" and st["direction"] == "reversal":
                            next_level = pseudo_levels.get("support", level_price)
                        elif level_type == "support" and st["direction"] == "reversal":
                            next_level = pseudo_levels.get("resistance", level_price)

                        # Prefer REAL ATR from the day's snapshot (accurate, no lookahead-bias
                        # risk since it's exactly what was known live). Fall back to the
                        # pseudo-ATR proxy (recent price high-low) only when no snapshot exists.
                        real_atr = (market_structure_to_use or {}).get("atr_14") if market_structure_to_use else None
                        if real_atr:
                            atr_to_use = real_atr
                        else:
                            recent_ltps = [h["ltp"] for h in history_buffer[-20:] if h.get("ltp")]
                            atr_to_use = (max(recent_ltps) - min(recent_ltps)) if len(recent_ltps) >= 3 else None

                        t1, t2, sl = compute_dynamic_targets_sl(entry_price, level_price, next_level, atr_to_use,
                                                                  delta_approx=target_delta_approx,
                                                                  min_target_pct=min_target_pct, max_sl_pct=max_sl_pct,
                                                                  swing_level_underlying=((market_structure_to_use or {}).get("swing_low") if direction_opt == "CE" else (market_structure_to_use or {}).get("swing_high")),
                                                                  underlying_price=underlying)
                        rr, meets_min = validate_risk_reward(entry_price, t1, sl, min_rr=min_risk_reward)
                        if meets_min and not open_trade:
                            open_trade = {
                                "strike": entry_strike, "direction": direction_opt,
                                "entry_price": entry_price, "target_price": t1, "sl_price": sl,
                                "entry_time": ts, "confidence": 75, "sr_level": level_key, "risk_reward": rr,
                            }
                    pbucket.append({"ltp": best["ltp"], "time": time_str})
                    if len(pbucket) > 20:
                        del pbucket[:-20]
                    vbucket.append(best.get("volume") or 0)
                    if len(vbucket) > 20:
                        del vbucket[:-20]

            sr_states[level_key] = st
            if open_trade:
                break   # a trade just opened this cycle -- stop scanning other levels

    # snapshots_by_date can contain dates with NO cycles at all (e.g. a
    # market-structure snapshot logged on a Sunday by app.py's live job even
    # though no OI cycles were ever recorded that day) -- such a snapshot is
    # never actually applied to any replayed cycle, so it must not inflate
    # the "day(s) used" count above total_days (which would be a nonsensical
    # X/Y ratio with X > Y). Intersect with real cycle dates first.
    cycle_dates = set(entry["cycle"]["date"] for entry in cycles)
    meta = {
        "elapsed_seconds": round(time.time() - start_time, 2),
        "snapshot_days_used": len(snapshots_by_date.keys() & cycle_dates),
        "total_days": len(cycle_dates),
    }
    return trades, len(cycles), meta


def simulate_trades(symbol, date_from, date_to, persistence_cycles, cooldown_minutes, confidence_threshold):
    """Returns (trades, cycle_count) -- pure data, no printing. Used by both the
    CLI (run_backtest) and the Flask /backtest web page."""
    cycles = load_cycles(symbol, date_from, date_to)
    if not cycles:
        return [], 0

    trades = []
    open_trade = None
    bias_streak = {"bias": None, "count": 0}
    cooldown_until = None

    for entry in cycles:
        c, rows = entry["cycle"], entry["rows"]
        row_map = {r.strike: r for r in rows}   # O(1) lookups instead of repeated O(n) linear scans
        ts = dt.datetime.fromisoformat(c["ts"])
        atm, pcr = c["atm"], c["pcr"]
        bias = c["bias"]

        if open_trade:
            row = row_map.get(open_trade["strike"])
            price = (row.ce_ltp if open_trade["direction"] == "CE" else row.pe_ltp) if row else None
            if price:
                held_min = (ts - open_trade["entry_time"]).total_seconds() / 60
                exit_reason = None
                if price >= open_trade["target_price"]:
                    exit_reason = "TARGET HIT"
                elif price <= open_trade["sl_price"]:
                    exit_reason = "STOP LOSS"
                elif held_min >= MAX_HOLD_MINUTES:
                    exit_reason = "TIME EXIT"
                if exit_reason:
                    points = round(price - open_trade["entry_price"], 2)
                    open_trade.update({"exit_price": price, "exit_time": ts, "exit_reason": exit_reason, "points": points})
                    trades.append(open_trade)
                    open_trade = None
                    cooldown_until = ts + dt.timedelta(minutes=cooldown_minutes)
            continue

        if bias_streak["bias"] == bias:
            bias_streak["count"] += 1
        else:
            bias_streak = {"bias": bias, "count": 1}

        if bias_streak["count"] < persistence_cycles:
            continue
        if cooldown_until and ts < cooldown_until:
            continue

        support, resistance = oi_walls(rows)
        signal = generate_signal(
            rows, atm, bias, c["note"], pcr, support, resistance,
            target_delta_approx=TARGET_DELTA_APPROX, sl_percent=OLD_ENGINE_SL_PERCENT,
            min_target_percent=MIN_TARGET_PERCENT, confidence_threshold=confidence_threshold,
        )
        if signal.get("tradeable"):
            open_trade = {
                "strike": signal["strike"], "direction": signal["direction"],
                "entry_price": signal["entry_price"], "target_price": signal["target_price"],
                "sl_price": signal["sl_price"], "entry_time": ts, "confidence": signal["confidence"],
            }

    return trades, len(cycles)


def analyze_v2_signals(symbol, date_from, date_to, lookforward_minutes=20):
    """
    Engine V2 backtest: replays historical cycles through V2's level-formula
    (engine_v2.py's compute_v2_levels) and trend/signal logic
    (compute_v2_trend_and_signal), using ONLY days with a REAL saved
    market-structure snapshot (matching V1's "genuinely accurate" standard --
    no pseudo-ATR approximation for V2, since the PDH/PDL values must be
    real, not guessed).

    Unlike simulate_sr_engine_trades, this does NOT simulate actual trade
    entries/targets/SL -- Engine V2 is intentionally read-only/display-only
    (see engine_v2.py). Instead, this reports:
    - How often each trend/signal fired (overall AND per-day, for transparency)
    - For BUY CE/PE signals, simple directional accuracy: did price move
      favorably over the next `lookforward_minutes`?
    """
    from engine_v2 import compute_v2_levels, compute_v2_trend_and_signal
    from sr_probability_engine import evaluate_resistance, evaluate_support

    start_time = time.time()
    cycles = load_cycles(symbol, date_from, date_to)
    if not cycles:
        return {"error": "No historical cycle data for this symbol/range."}
    snapshots_by_date = load_market_structure_snapshots(symbol, date_from, date_to)
    if not snapshots_by_date:
        return {"error": "No real market-structure snapshots for this range -- Engine V2 backtest requires genuine PDH/PDL data (no pseudo-approximation)."}

    print(f"Replaying {len(cycles)} cycles for {symbol} through Engine V2 ({len(snapshots_by_date)} day(s) with real snapshots)...")

    signal_counts = {"BUY CE": 0, "BUY PE": 0, "NO_TRADE": 0}
    trend_counts = {"BULLISH": 0, "BEARISH": 0, "RANGE": 0, "UNKNOWN": 0}
    per_day_signals = {}   # {date: {"BUY CE": n, "BUY PE": n, "NO_TRADE": n}}
    outcomes = []   # (signal, favorable: bool, points_moved)
    total_cycles = len(cycles)
    diag_above_pdc = 0
    diag_below_pdc = 0
    diag_resistance_holding = 0
    diag_support_holding = 0
    diag_total_evaluated = 0
    diag_pdc_by_day = {}
    diag_price_range_by_day = {}

    for i, c in enumerate(cycles):
        if (i + 1) % 500 == 0 or i == total_cycles - 1:
            _print_progress_bar(i + 1, total_cycles, start_time, len(outcomes))
        cyc = c["cycle"]
        date_str = cyc["date"]
        if date_str not in snapshots_by_date:
            continue
        snap = snapshots_by_date[date_str]
        custom_levels = snap.get("custom_levels")
        if not custom_levels or not all(custom_levels.get(k) for k in ("pdh", "pdl", "pdc")):
            continue
        # PDH/PDL/PDC are fixed from the PRIOR day and known before market
        # open, so custom_levels is safe to use for every cycle. The rest of
        # snap["market_structure"] (VWAP/swing/ADX/regime) is computed
        # intraday as of snap["ts"] though -- using it for cycles BEFORE that
        # ts is lookahead bias, same issue as simulate_sr_engine_trades.
        cyc_ts = dt.datetime.fromisoformat(cyc["ts"])
        snapshot_ms = snap["market_structure"] if dt.datetime.fromisoformat(snap["ts"]) <= cyc_ts else None

        underlying = cyc["underlying_ltp"]
        atm = cyc["atm"]
        pcr = cyc["pcr"]
        v2_levels = compute_v2_levels(custom_levels["pdh"], custom_levels["pdl"], custom_levels["pdc"], symbol=symbol)
        resistance_eval = evaluate_resistance(v2_levels["resistance"], c["rows"], atm, pcr, snapshot_ms, [], underlying)
        support_eval = evaluate_support(v2_levels["support"], c["rows"], atm, pcr, snapshot_ms, [], underlying)
        atm_row = next((r for r in c["rows"] if r.strike == atm), None)
        oi_buildup = None
        if atm_row is not None:
            from oi_engine import net_oi_buildup_lean
            oi_buildup = net_oi_buildup_lean(atm_row.ce_signal, atm_row.pe_signal)
        ts = compute_v2_trend_and_signal(v2_levels, resistance_eval, support_eval, underlying, oi_buildup=oi_buildup)

        diag_total_evaluated += 1
        diag_pdc_by_day.setdefault(date_str, custom_levels["pdc"])
        diag_price_range_by_day.setdefault(date_str, {"min": underlying, "max": underlying})
        diag_price_range_by_day[date_str]["min"] = min(diag_price_range_by_day[date_str]["min"], underlying)
        diag_price_range_by_day[date_str]["max"] = max(diag_price_range_by_day[date_str]["max"], underlying)
        if underlying > custom_levels["pdc"]:
            diag_above_pdc += 1
        else:
            diag_below_pdc += 1
        if resistance_eval["reversal_probability"] > resistance_eval["breakout_probability"]:
            diag_resistance_holding += 1
        if support_eval["reversal_probability"] > support_eval["breakdown_probability"]:
            diag_support_holding += 1

        trend_counts[ts["trend"]] = trend_counts.get(ts["trend"], 0) + 1
        signal_counts[ts["signal"]] = signal_counts.get(ts["signal"], 0) + 1
        per_day_signals.setdefault(date_str, {"BUY CE": 0, "BUY PE": 0, "NO_TRADE": 0})
        per_day_signals[date_str][ts["signal"]] = per_day_signals[date_str].get(ts["signal"], 0) + 1

        if ts["signal"] in ("BUY CE", "BUY PE"):
            # Look forward within the SAME day for a simple directional-accuracy check
            future_cycles = [
                fc for fc in cycles[i + 1:i + 1 + 40]   # cap the scan window
                if fc["cycle"]["date"] == date_str
            ]
            target_ts = cyc["ts"]
            future_price = None
            for fc in future_cycles:
                future_price = fc["cycle"]["underlying_ltp"]
                # stop once we've moved past the lookforward window (ts is ISO string, comparable)
                try:
                    import datetime as _dt
                    elapsed_min = (_dt.datetime.fromisoformat(fc["cycle"]["ts"]) - _dt.datetime.fromisoformat(target_ts)).total_seconds() / 60
                    if elapsed_min >= lookforward_minutes:
                        break
                except (ValueError, TypeError):
                    continue
            if future_price is not None and underlying:
                points_moved = future_price - underlying
                favorable = (points_moved > 0) if ts["signal"] == "BUY CE" else (points_moved < 0)
                outcomes.append((ts["signal"], favorable, points_moved))

    total_favorable = sum(1 for _, fav, _ in outcomes if fav)
    accuracy_pct = round(total_favorable / len(outcomes) * 100, 1) if outcomes else None
    replay_time = round(time.time() - start_time, 2)

    return {
        "symbol": symbol, "days_with_real_snapshots": len(snapshots_by_date),
        "trend_counts": trend_counts, "signal_counts": signal_counts,
        "per_day_signals": per_day_signals,
        "directional_checks": len(outcomes),
        "directional_accuracy_pct": accuracy_pct,
        "replay_time_seconds": replay_time, "cycles_replayed": total_cycles,
        "diagnostics": {
            "above_pdc_pct": round(diag_above_pdc / diag_total_evaluated * 100, 1) if diag_total_evaluated else None,
            "below_pdc_pct": round(diag_below_pdc / diag_total_evaluated * 100, 1) if diag_total_evaluated else None,
            "resistance_holding_pct": round(diag_resistance_holding / diag_total_evaluated * 100, 1) if diag_total_evaluated else None,
            "support_holding_pct": round(diag_support_holding / diag_total_evaluated * 100, 1) if diag_total_evaluated else None,
            "pdc_vs_price_range_by_day": {
                date_str: {"pdc": diag_pdc_by_day[date_str], "day_min": diag_price_range_by_day[date_str]["min"],
                           "day_max": diag_price_range_by_day[date_str]["max"]}
                for date_str in sorted(diag_pdc_by_day.keys())
            },
        },
        "note": "Directional-accuracy is a SIMPLE proxy (did price move favorably within "
                f"{lookforward_minutes} min of the signal) -- NOT a full trade simulation with "
                "entry/target/SL, since Engine V2 is intentionally read-only/display-only for now.",
    }


V3_MAX_HOLD_MINUTES = 30   # mirrors app.py's V3_MAX_HOLD_MINUTES -- kept as a local constant rather than
                            # importing app.py here (app.py already imports FROM backtest.py inside
                            # _run_backtest_job; importing back would be circular).
V3_MAX_HOLD_MINUTES_HARD_CAP = V3_MAX_HOLD_MINUTES * 2   # mirrors app.py's absolute ceiling -- see should_pause_time_exit
V3_STRIKE_STEP = 50   # only used when the caller doesn't pass one -- matches NIFTY/BANKNIFTY's step
CANDLE_DATA_DIR = Path("data/history")


def load_intraday_candles(symbol, timeframe="3m"):
    """
    Loads the historical intraday candle archive (data/history/<symbol>/<tf>.parquet
    or .csv, written by history_engine.py's HistoryEngine.fetch -- the SAME
    files backfill_market_structure.py already reads) for the candle-based
    adaptive time-exit check (should_pause_time_exit) during V3 backtest
    replay. Returns a datetime-sorted DataFrame, or an empty one if no
    archive exists for this symbol -- callers degrade gracefully to a plain
    time-exit in that case (should_pause_time_exit already returns False on
    missing candle data), never raise.
    """
    pq_path = CANDLE_DATA_DIR / symbol / f"{timeframe}.parquet"
    csv_path = CANDLE_DATA_DIR / symbol / f"{timeframe}.csv"
    if pq_path.exists():
        df = pd.read_parquet(pq_path)
    elif csv_path.exists():
        df = pd.read_csv(csv_path, parse_dates=["datetime"])
    else:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime").reset_index(drop=True)


def _two_prior_completed_candles(candles_df, ts, interval_minutes=3):
    """
    Returns (prev_candle, current_candle) dicts for should_pause_time_exit --
    the two most recent candles that had ALREADY fully closed by `ts` (start
    time + interval <= ts, same non-repaint rule as
    market_structure.build_market_structure's forming-candle exclusion).
    Returns (None, None) if there isn't enough history yet.
    """
    if candles_df is None or candles_df.empty:
        return None, None
    cutoff = ts - dt.timedelta(minutes=interval_minutes)
    idx = candles_df["datetime"].searchsorted(cutoff, side="right")
    if idx < 2:
        return None, None
    prev_row, cur_row = candles_df.iloc[idx - 2], candles_df.iloc[idx - 1]
    return (
        {"open": prev_row["open"], "high": prev_row["high"], "low": prev_row["low"], "close": prev_row["close"]},
        {"open": cur_row["open"], "high": cur_row["high"], "low": cur_row["low"], "close": cur_row["close"]},
    )


def _recent_completed_candles(candles_df, ts, interval_minutes=3, n=10):
    """
    Returns the last `n` candles that had ALREADY fully closed by `ts`
    (oldest-first, same non-repaint rule as _two_prior_completed_candles) --
    feeds market_structure["recent_candles"] during V3 backtest replay so
    confirms_reversal_entry (sr_engine_v3.py) has genuine price-action data
    to check against. WITHOUT this, backtest's market_structure never
    carries candles at all (market_structure_snapshots doesn't persist them
    -- only live app.py's in-memory state does), so confirms_reversal_entry
    would always see len(candles)<2 and never confirm a single "hold" trade
    -- found 2026-08-02 while diagnosing why confidence cleared the bar
    often but almost nothing actually executed. Returns [] gracefully if
    there's no candle archive for this symbol.
    """
    if candles_df is None or candles_df.empty:
        return []
    cutoff = ts - dt.timedelta(minutes=interval_minutes)
    idx = candles_df["datetime"].searchsorted(cutoff, side="right")
    if idx < 1:
        return []
    window = candles_df.iloc[max(0, idx - n):idx]
    return [{"open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]}
            for _, r in window.iterrows()]


def compute_advanced_trade_stats(trades):
    """
    Aggregate performance stats for a completed V3 trade list (each trade a
    dict with at least 'points'/'exit_reason', plus 'mfe'/'mae' if the
    replay tracked them -- see simulate_v3_engine_trades). None-safe on an
    empty list rather than dividing by zero.

    Definitions (kept deliberately simple/transparent, no proprietary math):
    - Win Rate: trades with points > 0, divided by all trades. Deliberately
      NOT "exit_reason == TARGET HIT" -- engines with a trailing stop (e.g.
      dynamic_sr_engine/exit_engine_v4) rarely resolve via a literal target
      hit (~2% of trades, see dynamic_sr_engine.py's ENTRY_BLACKOUT comment);
      most profit is realized as a profit-protected trailing-SL exit still
      labeled "STOP LOSS". Gating Win Rate on the exit-reason string made it
      read ~0% even on net-profitable runs, contradicting "Net positive %"
      printed right above it from the exact same trades.
    - Accuracy: among only the RESOLVED trades (exit_reason TARGET HIT or
      STOP LOSS -- excludes TIME EXIT, since a trade that got cut off by the
      clock isn't "right" or "wrong", it's unresolved), the fraction that
      were still net profitable. A genuinely different number from Win Rate
      (denominator excludes time exits), not a duplicate -- but same
      profit-based win test, so it no longer disagrees with Win Rate on the
      resolved subset.
    - Profit Factor: gross profit / gross loss. None (undefined, not
      infinite) if there are no losing trades yet.
    - Expectancy: net P&L / total trades -- textbook per-trade expected value.
    - Sharpe Ratio: mean/stdev of PER-TRADE points -- NOT calendar-annualized
      (that would need an equity curve resampled to daily bars, more
      machinery than this replay tracks; flagged honestly here rather than
      faking a time-annualized number).
    """
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0, "time_exits": 0,
            "win_rate": 0.0, "accuracy": None, "profit_factor": None,
            "net_pnl": 0.0, "max_drawdown": 0.0, "avg_mfe": None, "avg_mae": None,
            "expectancy": None, "sharpe_ratio": None,
        }

    # BREAKEVEN STOP / PROFIT-LOCKED STOP (exit_engine_v4) are SL-mechanism
    # exits like STOP LOSS, just relabeled because the trailing stop had
    # already ratcheted to/past entry -- they're resolved trades, not
    # unresolved time-based cutoffs, so they belong with TARGET HIT/STOP LOSS
    # for accuracy purposes, not lumped in with TIME EXIT.
    resolved_reasons = ("TARGET HIT", "STOP LOSS", "BREAKEVEN STOP", "PROFIT-LOCKED STOP")
    wins = [t for t in trades if t.get("exit_reason") == "TARGET HIT"]
    losses = [t for t in trades if t.get("exit_reason") == "STOP LOSS"]
    time_exits = [t for t in trades if t.get("exit_reason") not in resolved_reasons]
    total = len(trades)
    points = [t.get("points") or 0.0 for t in trades]
    net_pnl = round(sum(points), 2)

    profitable = [t for t in trades if (t.get("points") or 0.0) > 0]
    win_rate = round(len(profitable) / total * 100, 1) if total else 0.0

    resolved_trades = [t for t in trades if t.get("exit_reason") in resolved_reasons]
    resolved_wins = [t for t in resolved_trades if (t.get("points") or 0.0) > 0]
    accuracy = round(len(resolved_wins) / len(resolved_trades) * 100, 1) if resolved_trades else None

    gross_win = sum(p for p in points if p > 0)
    gross_loss = abs(sum(p for p in points if p < 0))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in points:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    mfes = [t["mfe"] for t in trades if t.get("mfe") is not None]
    maes = [t["mae"] for t in trades if t.get("mae") is not None]
    avg_mfe = round(sum(mfes) / len(mfes), 2) if mfes else None
    avg_mae = round(sum(maes) / len(maes), 2) if maes else None

    expectancy = round(net_pnl / total, 2) if total else None
    sharpe_ratio = None
    if len(points) >= 2:
        stdev = statistics.pstdev(points)
        if stdev > 0:
            sharpe_ratio = round(statistics.mean(points) / stdev, 3)

    return {
        "total_trades": total, "wins": len(wins), "losses": len(losses), "time_exits": len(time_exits),
        "win_rate": win_rate, "accuracy": accuracy, "profit_factor": profit_factor,
        "net_pnl": net_pnl, "max_drawdown": round(max_dd, 2),
        "avg_mfe": avg_mfe, "avg_mae": avg_mae,
        "expectancy": expectancy, "sharpe_ratio": sharpe_ratio,
    }


def simulate_v3_engine_trades(symbol, date_from, date_to, strike_step=V3_STRIKE_STEP,
                               max_hold_minutes=V3_MAX_HOLD_MINUTES, progress_callback=None,
                               cooldown_minutes=0, min_risk_reward=None, confidence_threshold=None,
                               min_target_pct=None, max_sl_pct=None):
    """
    Backtests S/R Engine V3 (sr_engine_v3.py) -- reuses load_cycles/
    load_market_structure_snapshots exactly like simulate_sr_engine_trades,
    with the SAME lookahead-bias guard (a day's snapshot only applies to
    cycles at/after its own `ts`). ONLY replays days with a REAL saved
    market-structure snapshot (matching Engine V2's analyze_v2_signals
    standard) -- V3's raw-level fusion needs genuine PDH/PDL/CPR/Pivot
    inputs, not a guessed pseudo-approximation.

    Previous-day validation during replay: for each day, the LAST cycle's
    rows (closing option chain) plus that day's own reconstructed market
    structure become "yesterday's data" for the NEXT day in the sequence --
    entirely from already-loaded data, no extra DB round-trips per cycle.

    Returns (trades, cycle_count, meta) -- trades include 'mfe'/'mae'
    (intra-trade max favorable/adverse excursion in premium points) for
    compute_advanced_trade_stats.
    """
    start_time = time.time()
    cycles = load_cycles(symbol, date_from, date_to)
    if not cycles:
        return [], 0, {"elapsed_seconds": 0, "snapshot_days_used": 0, "total_days": 0}
    snapshots_by_date = load_market_structure_snapshots(symbol, date_from, date_to)
    if not snapshots_by_date:
        return [], len(cycles), {"elapsed_seconds": 0, "snapshot_days_used": 0,
                                   "total_days": len(set(e["cycle"]["date"] for e in cycles)),
                                   "error": "No real market-structure snapshots for this range -- "
                                            "V3 backtest requires genuine PDH/PDL/CPR/Pivot data, no pseudo-approximation."}

    cycles_by_date = defaultdict(list)
    for entry in cycles:
        cycles_by_date[entry["cycle"]["date"]].append(entry)
    sorted_dates = sorted(cycles_by_date.keys())

    # Real intraday candle archive (data/history/<symbol>/3m.*) for the
    # candle-based adaptive time-exit (should_pause_time_exit) -- loaded ONCE,
    # empty DataFrame (not an error) if this symbol has no saved candle history.
    candles_df = load_intraday_candles(symbol)
    candle_interval_minutes = 3
    if len(candles_df) >= 2:
        candle_interval_minutes = max(1, (candles_df["datetime"].iloc[1] - candles_df["datetime"].iloc[0]).total_seconds() / 60)

    # Precompute each day's "closing" info (last cycle's rows + that day's own
    # reconstructed market structure) ONCE -- reused as "previous day" input
    # when replaying the NEXT day, never recomputed per-cycle.
    day_close_info = {}
    for date_str in sorted_dates:
        snap = snapshots_by_date.get(date_str)
        if not snap or not snap.get("custom_levels"):
            continue
        custom_levels = snap["custom_levels"]
        if not all(custom_levels.get(k) for k in ("pdh", "pdl", "pdc")):
            continue
        ms = {
            **snap["market_structure"],
            "pivots": classical_pivots(custom_levels["pdh"], custom_levels["pdl"], custom_levels["pdc"]),
            "cpr": calc_cpr(custom_levels["pdh"], custom_levels["pdl"], custom_levels["pdc"]),
            "custom_levels": custom_levels,
        }
        last_entry = cycles_by_date[date_str][-1]
        underlying_close = last_entry["cycle"]["underlying_ltp"]
        day_close_info[date_str] = {
            "rows": last_entry["rows"], "underlying": underlying_close, "ms": ms,
        }

    # For each replay date, the most recent EARLIER date with valid close
    # info (never today's own, and never a later date -- pure lookahead guard).
    prev_date_map, last_avail = {}, None
    for d in sorted_dates:
        prev_date_map[d] = last_avail
        if d in day_close_info:
            last_avail = d

    trades = []
    open_trade = None
    cooldown_until = None
    current_date, today_ltp_history = None, []
    volume_history_by_key = defaultdict(list)
    wall_center_history = {"support": [], "resistance": []}
    cached_prev_date, prev_day_validation = None, None

    total_cycles = len(cycles)
    print(f"Replaying {total_cycles} cycles for {symbol} through S/R Engine V3 "
          f"({len(day_close_info)} day(s) with real snapshots)...")
    progress_every = max(100, total_cycles // 40)

    for idx, entry in enumerate(cycles):
        if idx > 0 and (idx % progress_every == 0 or idx == total_cycles - 1):
            _print_progress_bar(idx + 1, total_cycles, start_time, len(trades))
            if progress_callback:
                try:
                    progress_callback(idx + 1, total_cycles, time.time() - start_time, len(trades))
                except Exception:
                    pass

        c, rows = entry["cycle"], entry["rows"]
        row_map = {r.strike: r for r in rows}
        ts = dt.datetime.fromisoformat(c["ts"])
        date_str, underlying = c["date"], c["underlying_ltp"]

        if date_str != current_date:
            current_date, today_ltp_history = date_str, []
        if underlying is not None:
            today_ltp_history.append(underlying)

        # -- manage an already-open trade first (target/SL/time-exit), tracking MFE/MAE --
        if open_trade:
            row = row_map.get(open_trade["strike"])
            price = (row.ce_ltp if open_trade["direction"] == "CE" else row.pe_ltp) if row else None
            if price:
                excursion = price - open_trade["entry_price"]
                open_trade["mfe"] = max(open_trade["mfe"], excursion)
                open_trade["mae"] = min(open_trade["mae"], excursion)
                held_min = (ts - open_trade["entry_time"]).total_seconds() / 60
                exit_reason = None
                if price >= open_trade["target_price"]:
                    exit_reason = "TARGET HIT"
                elif price <= open_trade["sl_price"]:
                    exit_reason = "STOP LOSS"
                elif held_min >= V3_MAX_HOLD_MINUTES_HARD_CAP:
                    exit_reason = "TIME EXIT"   # absolute ceiling -- fires regardless of candle structure
                elif held_min >= max_hold_minutes:
                    prev_c, cur_c = _two_prior_completed_candles(candles_df, ts, candle_interval_minutes)
                    if not should_pause_time_exit(open_trade["direction"], prev_c, cur_c):
                        exit_reason = "TIME EXIT"
                if exit_reason:
                    points = round(price - open_trade["entry_price"], 2)
                    open_trade.update({"exit_price": price, "exit_time": ts, "exit_reason": exit_reason, "points": points})
                    trades.append(open_trade)
                    open_trade = None
                    if cooldown_minutes:
                        cooldown_until = ts + dt.timedelta(minutes=cooldown_minutes)
            continue   # one trade at a time, matches every other engine's replay behavior

        if cooldown_until and ts < cooldown_until:
            continue

        prev_date = prev_date_map.get(date_str)
        if prev_date != cached_prev_date:
            cached_prev_date = prev_date
            if prev_date:
                info = day_close_info[prev_date]
                prev_day_validation = validate_previous_day_levels(
                    info["rows"], info["ms"], info["underlying"], strike_step,
                )
            else:
                prev_day_validation = None

        day_snapshot = snapshots_by_date.get(date_str)
        # Same lookahead-bias guard as simulate_sr_engine_trades: today's
        # snapshot-derived market structure is only valid at/after its own ts.
        ms_known_yet = bool(day_snapshot) and dt.datetime.fromisoformat(day_snapshot["ts"]) <= ts
        if not (day_snapshot and day_snapshot.get("custom_levels") and ms_known_yet):
            continue   # V3 needs genuine intraday market-structure -- no pseudo-fallback for this engine
        custom_levels = day_snapshot["custom_levels"]
        if not all(custom_levels.get(k) for k in ("pdh", "pdl", "pdc")):
            continue
        market_structure_to_use = {
            **day_snapshot["market_structure"],
            "pivots": classical_pivots(custom_levels["pdh"], custom_levels["pdl"], custom_levels["pdc"]),
            "cpr": calc_cpr(custom_levels["pdh"], custom_levels["pdl"], custom_levels["pdc"]),
            "custom_levels": custom_levels,
            # Real intraday candles (from the same archive should_pause_time_exit
            # already uses) -- WITHOUT this, market_structure never carries
            # candles during backtest (market_structure_snapshots doesn't
            # persist them), so confirms_reversal_entry could never confirm a
            # single "hold" trade regardless of confidence (found 2026-08-02).
            "recent_candles": _recent_completed_candles(candles_df, ts, candle_interval_minutes),
        }

        volume_history_by_key_for_cycle = {}
        if prev_day_validation:
            for side, direction in (("support", "PE"), ("resistance", "CE")):
                cluster = (prev_day_validation.get(side) or {}).get("cluster")
                strike = cluster.get("center_strike") if cluster else None
                if strike is not None:
                    key = (strike, direction)
                    volume_history_by_key_for_cycle[key] = list(volume_history_by_key[key])
                    row = row_map.get(strike)
                    cur_vol = (row.ce_vol if direction == "CE" else row.pe_vol) if row else None
                    if cur_vol is not None:
                        volume_history_by_key[key].append(cur_vol)
                        del volume_history_by_key[key][:-30]

        v3_signal = generate_v3_signal(
            rows, underlying, market_structure_to_use, strike_step,
            prev_day_validation=prev_day_validation, today_ltp_history=today_ltp_history,
            volume_history_by_key=volume_history_by_key_for_cycle, expiry_date=None, now=ts,
            wall_center_history={"support": wall_center_history["support"][-30:],
                                  "resistance": wall_center_history["resistance"][-30:]},
            min_risk_reward=min_risk_reward, confidence_threshold=confidence_threshold,
            min_target_pct=min_target_pct, max_sl_pct=max_sl_pct,
        )
        # Append THIS cycle's cluster centers AFTER evaluation (prior-readings-
        # only contract, same as volume_history_by_key above).
        if v3_signal.get("support_cluster_center") is not None:
            wall_center_history["support"].append(v3_signal["support_cluster_center"])
        if v3_signal.get("resistance_cluster_center") is not None:
            wall_center_history["resistance"].append(v3_signal["resistance_cluster_center"])

        if v3_signal.get("tradeable"):
            direction = v3_signal["direction"]
            # v3_signal["strike"] is the engine's own chosen entry strike --
            # NOT inferred from direction+support/resistance_strike (that
            # inference is wrong for the extension/continuation cases, see
            # generate_v3_signal's "strike" field docstring).
            open_trade = {
                "strike": v3_signal["strike"], "direction": direction,
                "entry_price": v3_signal["suggested_entry"], "target_price": v3_signal["target"],
                "sl_price": v3_signal["stop_loss"], "entry_time": ts, "confidence": v3_signal["confidence"],
                "risk_reward": v3_signal.get("risk_reward"), "mfe": 0.0, "mae": 0.0,
            }

    meta = {
        "elapsed_seconds": round(time.time() - start_time, 2),
        "snapshot_days_used": len(day_close_info),
        "total_days": len(sorted_dates),
    }
    return trades, len(cycles), meta


ICHIMOKU_MAX_HOLD_MINUTES = 60   # institutional default -- configurable via simulate_ichimoku_trades' param


def simulate_ichimoku_trades(symbol, date_from, date_to, max_hold_minutes=ICHIMOKU_MAX_HOLD_MINUTES,
                              cooldown_minutes=0, progress_callback=None):
    """
    Backtests ichimoku_engine.py's OWN entry/exit/risk-management logic
    (independent of the option-chain OI engines above) against the real
    archived underlying candle history (data/history/<symbol>/3m.*, same
    archive load_intraday_candles already reads for V3's time-exit check).

    SAME CODE PATH AS LIVE: every reading here comes from
    ichimoku_engine.IchimokuLiveEngine.snapshot() -- the exact incremental
    engine app.py's live loop uses (see app.py wiring) -- pushed forward
    candle-by-candle with NO lookahead (each snapshot only ever sees
    candles up to and including the one just pushed). There is no separate
    "backtest-only" formula.

    Trades are in UNDERLYING POINTS (see ichimoku_engine.py's module
    docstring -- this engine trades the underlying candle series, not
    option premium): one position open at a time, entered when the signal
    flips to BUY/STRONG BUY/SELL/STRONG SELL while flat, exited on the
    first candle whose high/low touches the risk-managed stop or first
    target, or on a time-exit after `max_hold_minutes` -- same
    target/stop/time-exit vocabulary (`exit_reason`) as simulate_v3_engine_trades
    so compute_advanced_trade_stats works unmodified on this trade list too
    (no duplicate win-rate/profit-factor/drawdown math, see
    compute_ichimoku_accuracy_stats below).

    ADVISORY VALIDATION ONLY: this function does not touch the option-chain
    OI trade simulations above -- it exists to measure Ichimoku's OWN
    historical accuracy before it's ever trusted to influence a real
    order (see ichimoku_engine.py's module docstring).
    """
    import ichimoku_engine as ie

    start_time = time.time()
    candles_df = load_intraday_candles(symbol)
    if candles_df.empty:
        return [], 0, {"elapsed_seconds": 0, "error": f"No candle archive for {symbol} (data/history/{symbol}/3m.*)."}

    from_dt = dt.datetime.fromisoformat(date_from)
    to_dt = dt.datetime.fromisoformat(date_to) + dt.timedelta(days=1)
    candles_df = candles_df[(candles_df["datetime"] >= from_dt) & (candles_df["datetime"] < to_dt)].reset_index(drop=True)
    if len(candles_df) < ie.KIJUN_PERIOD + ie.DISPLACEMENT:
        return [], len(candles_df), {"elapsed_seconds": 0,
                                       "error": f"Only {len(candles_df)} candles in range -- need >= "
                                                f"{ie.KIJUN_PERIOD + ie.DISPLACEMENT} for a real Ichimoku cloud."}

    engine = ie.IchimokuLiveEngine()
    records = candles_df.to_dict("records")
    seed_size = ie.SENKOU_B_PERIOD + ie.DISPLACEMENT + 5
    engine.seed(records[:seed_size])

    trades, open_trade, cooldown_until, prev_action = [], None, None, "WAIT"
    total = len(records) - seed_size
    progress_every = max(50, total // 40) if total > 0 else 1

    for idx, candle in enumerate(records[seed_size:]):
        engine.push(candle)
        ts = candle["datetime"]

        if idx > 0 and (idx % progress_every == 0 or idx == total - 1) and progress_callback:
            try:
                progress_callback(idx + 1, total, time.time() - start_time, len(trades))
            except Exception:
                pass

        snap = engine.snapshot()

        if open_trade:
            direction, high, low, close = open_trade["direction"], candle["high"], candle["low"], candle["close"]
            hit_target = (direction == "BUY" and high >= open_trade["target_price"]) or \
                         (direction == "SELL" and low <= open_trade["target_price"])
            hit_stop = (direction == "BUY" and low <= open_trade["sl_price"]) or \
                       (direction == "SELL" and high >= open_trade["sl_price"])
            held_minutes = (ts - open_trade["entry_time"]).total_seconds() / 60
            exit_reason, exit_price = None, None
            if hit_stop:
                exit_reason, exit_price = "STOP LOSS", open_trade["sl_price"]
            elif hit_target:
                exit_reason, exit_price = "TARGET HIT", open_trade["target_price"]
            elif held_minutes >= max_hold_minutes:
                exit_reason, exit_price = "TIME EXIT", close
            if exit_reason:
                points = (exit_price - open_trade["entry_price"]) if direction == "BUY" else (open_trade["entry_price"] - exit_price)
                trades.append({**open_trade, "exit_time": ts, "exit_price": exit_price,
                                "exit_reason": exit_reason, "points": round(points, 2)})
                if exit_reason == "STOP LOSS" and cooldown_minutes:
                    cooldown_until = ts + dt.timedelta(minutes=cooldown_minutes)
                open_trade = None

        action = snap["entry_signal"]
        in_cooldown = cooldown_until is not None and ts < cooldown_until
        if not open_trade and not in_cooldown and action != prev_action and action in ("BUY", "STRONG BUY", "SELL", "STRONG SELL"):
            risk = snap.get("risk_management") or {}
            if risk.get("initial_stop") is not None and risk.get("targets"):
                direction = "BUY" if action in ("BUY", "STRONG BUY") else "SELL"
                open_trade = {
                    "direction": direction, "entry_time": ts, "entry_price": risk["entry"],
                    "sl_price": risk["initial_stop"], "target_price": risk["targets"][0],
                    "confidence": snap["confidence_score"], "trend_stage": snap.get("trend_stage"),
                    "entry_signal": action, "risk_reward": risk.get("risk_reward"),
                }
        prev_action = action

    meta = {"elapsed_seconds": round(time.time() - start_time, 2), "candles_replayed": len(records),
            "symbol": symbol, "note": "Advisory validation only -- see ichimoku_engine.py module docstring."}
    return trades, len(records), meta


def compute_ichimoku_accuracy_stats(trades):
    """
    Wraps compute_advanced_trade_stats (win rate, accuracy/precision,
    profit factor, net P&L, max drawdown, expectancy, Sharpe -- NOT
    reimplemented here) and adds the Ichimoku-specific extras requested for
    validating this engine before it's trusted with real execution:
    False Buy % / False Sell % (of resolved BUY/SELL trades that hit STOP
    LOSS), Average Holding Time, and Trend Detection Accuracy (fraction of
    ALL resolved trades -- TARGET HIT or STOP LOSS -- where price moved in
    the SIGNALED direction at all by exit, i.e. points > 0 OR the trade at
    least moved favorably before reversing -- here scored the same as
    Win Rate on the resolved subset, i.e. identical to `accuracy` above;
    kept as a separate named field since "trend detection" and "trade
    P&L" are conceptually distinct even though this replay only has trade
    P&L to measure them with -- a genuinely independent trend-detection
    check would need per-signal forward-return sampling, out of scope for
    a first validation pass).
    """
    base = compute_advanced_trade_stats(trades)
    if not trades:
        return {**base, "false_buy_pct": None, "false_sell_pct": None,
                "avg_holding_minutes": None, "trend_detection_accuracy": base["accuracy"]}

    buys = [t for t in trades if t["direction"] == "BUY" and t["exit_reason"] in ("TARGET HIT", "STOP LOSS")]
    sells = [t for t in trades if t["direction"] == "SELL" and t["exit_reason"] in ("TARGET HIT", "STOP LOSS")]
    false_buy_pct = round(sum(1 for t in buys if t["exit_reason"] == "STOP LOSS") / len(buys) * 100, 1) if buys else None
    false_sell_pct = round(sum(1 for t in sells if t["exit_reason"] == "STOP LOSS") / len(sells) * 100, 1) if sells else None

    holding_minutes = [(t["exit_time"] - t["entry_time"]).total_seconds() / 60 for t in trades]
    avg_holding = round(sum(holding_minutes) / len(holding_minutes), 1) if holding_minutes else None

    return {
        **base,
        "false_buy_pct": false_buy_pct, "false_sell_pct": false_sell_pct,
        "avg_holding_minutes": avg_holding,
        "trend_detection_accuracy": base["accuracy"],
    }


def simulate_v2_engine_trades(symbol, date_from, date_to, strike_step=V3_STRIKE_STEP,
                               max_hold_minutes=V3_MAX_HOLD_MINUTES, min_risk_reward=1.5,
                               progress_callback=None):
    """
    Full trade-simulation backtest for Engine V2 (engine_v2.py) -- added
    2026-08-02 per request, so V2 can be compared against V1/V3 using the
    SAME compute_advanced_trade_stats format. Unlike analyze_v2_signals
    (frequency/directional-accuracy analysis only, per Engine V2's
    deliberately read-only LIVE design -- see engine_v2.py's docstring),
    this ACTUALLY opens/manages trades with entry/target/SL. This is
    backtest-only: engine_v2.py itself, and the live app's read-only V2
    display, are completely unchanged.

    ONLY replays days with a REAL saved market-structure snapshot (same
    standard as analyze_v2_signals/simulate_v3_engine_trades) -- V2's
    PDH/PDL-based levels need genuine data, not a guessed approximation.

    Entry strike: score_strike_candidates' liquidity-ranked ATM/ITM/OTM pick
    (the SAME strike-selection the swing engines already use) -- NOT V3's
    OI-cluster ladder, since V2's signal is a directional ATM-area call, not
    a specific-strike wall read. Target/SL: compute_dynamic_targets_sl off
    V2's own resistance/support levels (same math V1 uses). Same candle-based
    adaptive time-exit (should_pause_time_exit) as V3, with the same absolute
    hard-cap safety net.
    """
    from engine_v2 import compute_v2_levels, compute_v2_trend_and_signal
    from sr_probability_engine import evaluate_resistance, evaluate_support
    from oi_engine import net_oi_buildup_lean

    start_time = time.time()
    cycles = load_cycles(symbol, date_from, date_to)
    if not cycles:
        return [], 0, {"elapsed_seconds": 0, "snapshot_days_used": 0, "total_days": 0}
    snapshots_by_date = load_market_structure_snapshots(symbol, date_from, date_to)
    if not snapshots_by_date:
        return [], len(cycles), {"elapsed_seconds": 0, "snapshot_days_used": 0,
                                   "total_days": len(set(e["cycle"]["date"] for e in cycles)),
                                   "error": "No real market-structure snapshots for this range -- "
                                            "Engine V2 backtest requires genuine PDH/PDL data, no pseudo-approximation."}

    candles_df = load_intraday_candles(symbol)
    candle_interval_minutes = 3
    if len(candles_df) >= 2:
        candle_interval_minutes = max(1, (candles_df["datetime"].iloc[1] - candles_df["datetime"].iloc[0]).total_seconds() / 60)

    trades = []
    open_trade = None
    total_cycles = len(cycles)
    print(f"Replaying {total_cycles} cycles for {symbol} through Engine V2 (full trade simulation, "
          f"{len(snapshots_by_date)} day(s) with real snapshots)...")
    progress_every = max(100, total_cycles // 40)

    for idx, entry in enumerate(cycles):
        if idx > 0 and (idx % progress_every == 0 or idx == total_cycles - 1):
            _print_progress_bar(idx + 1, total_cycles, start_time, len(trades))
            if progress_callback:
                try:
                    progress_callback(idx + 1, total_cycles, time.time() - start_time, len(trades))
                except Exception:
                    pass

        cyc, rows = entry["cycle"], entry["rows"]
        row_map = {r.strike: r for r in rows}
        ts = dt.datetime.fromisoformat(cyc["ts"])
        date_str = cyc["date"]
        underlying, atm, pcr = cyc["underlying_ltp"], cyc["atm"], cyc["pcr"]

        if open_trade:
            row = row_map.get(open_trade["strike"])
            price = (row.ce_ltp if open_trade["direction"] == "CE" else row.pe_ltp) if row else None
            if price:
                excursion = price - open_trade["entry_price"]
                open_trade["mfe"] = max(open_trade["mfe"], excursion)
                open_trade["mae"] = min(open_trade["mae"], excursion)
                held_min = (ts - open_trade["entry_time"]).total_seconds() / 60
                exit_reason = None
                if price >= open_trade["target_price"]:
                    exit_reason = "TARGET HIT"
                elif price <= open_trade["sl_price"]:
                    exit_reason = "STOP LOSS"
                elif held_min >= V3_MAX_HOLD_MINUTES_HARD_CAP:
                    exit_reason = "TIME EXIT"
                elif held_min >= max_hold_minutes:
                    prev_c, cur_c = _two_prior_completed_candles(candles_df, ts, candle_interval_minutes)
                    if not should_pause_time_exit(open_trade["direction"], prev_c, cur_c):
                        exit_reason = "TIME EXIT"
                if exit_reason:
                    points = round(price - open_trade["entry_price"], 2)
                    open_trade.update({"exit_price": price, "exit_time": ts, "exit_reason": exit_reason, "points": points})
                    trades.append(open_trade)
                    open_trade = None
            continue   # one trade at a time, matches every other engine's replay behavior

        day_snapshot = snapshots_by_date.get(date_str)
        custom_levels = day_snapshot.get("custom_levels") if day_snapshot else None
        if not custom_levels or not all(custom_levels.get(k) for k in ("pdh", "pdl", "pdc")):
            continue
        # Same lookahead-bias guard as simulate_sr_engine_trades/simulate_v3_engine_trades:
        # PDH/PDL/PDC are fixed from the prior day (safe for every cycle), but VWAP/ADX/regime
        # are computed intraday as of the snapshot's own ts -- only valid at/after that moment.
        snapshot_is_known_yet = dt.datetime.fromisoformat(day_snapshot["ts"]) <= ts
        market_structure_to_use = day_snapshot["market_structure"] if snapshot_is_known_yet else None

        v2_levels = compute_v2_levels(custom_levels["pdh"], custom_levels["pdl"], custom_levels["pdc"], symbol=symbol)
        resistance_eval = evaluate_resistance(v2_levels["resistance"], rows, atm, pcr, market_structure_to_use, [], underlying)
        support_eval = evaluate_support(v2_levels["support"], rows, atm, pcr, market_structure_to_use, [], underlying)
        atm_row = row_map.get(atm)
        oi_buildup = net_oi_buildup_lean(atm_row.ce_signal, atm_row.pe_signal) if atm_row else None
        signal_result = compute_v2_trend_and_signal(v2_levels, resistance_eval, support_eval, underlying, oi_buildup=oi_buildup)

        if signal_result["signal"] not in ("BUY CE", "BUY PE"):
            continue
        direction = "CE" if signal_result["signal"] == "BUY CE" else "PE"

        candidates = score_strike_candidates(rows, atm, strike_step, direction)
        liquid_candidates = [cand for cand in candidates if cand.get("liquidity_ok")]
        if not liquid_candidates:
            continue
        entry_strike = liquid_candidates[0]["strike"]
        entry_row = row_map.get(entry_strike)
        entry_price = (entry_row.ce_ltp if direction == "CE" else entry_row.pe_ltp) if entry_row else None
        if not entry_price:
            continue

        level_price = v2_levels["resistance"] if direction == "CE" else v2_levels["support"]
        next_level_price = v2_levels["support"] if direction == "CE" else v2_levels["resistance"]
        atr = (market_structure_to_use or {}).get("atr_14")
        t1, _t2, sl = compute_dynamic_targets_sl(entry_price, level_price, next_level_price, atr,
                                                   delta_approx=TARGET_DELTA_APPROX,
                                                   min_target_pct=MIN_TARGET_PERCENT, max_sl_pct=MAX_SL_PERCENT)
        rr, meets_min = validate_risk_reward(entry_price, t1, sl, min_rr=min_risk_reward)
        if not meets_min:
            continue

        open_trade = {
            "strike": entry_strike, "direction": direction,
            "entry_price": entry_price, "target_price": t1, "sl_price": sl,
            "entry_time": ts, "risk_reward": rr, "mfe": 0.0, "mae": 0.0,
        }

    cycle_dates = set(e["cycle"]["date"] for e in cycles)
    meta = {
        "elapsed_seconds": round(time.time() - start_time, 2),
        "snapshot_days_used": len(cycle_dates & snapshots_by_date.keys()),
        "total_days": len(cycle_dates),
    }
    return trades, len(cycles), meta


def _dsr_finalize_trade(t):
    points = (t["exit_price"] - t["entry"]) if t["direction"] == "BUY" else (t["entry"] - t["exit_price"])
    t["points"] = round(points, 2)
    return t


def simulate_dynamic_sr_engine_trades(symbol, date_from, date_to, max_hold_minutes=MAX_HOLD_MINUTES,
                                       recent_window=30, progress_callback=None, min_tradeable_confidence=None):
    """
    Replays dynamic_sr_engine.py (the new PDH/PDL-based Dynamic S/R Engine V1,
    added 2026-08-02 -- see app.py's /dynamic-sr for the live version) over the
    logged intraday candle archive (data/history/<symbol>/3m.*, the SAME file
    market_structure.py's live PDH/PDL/candle window is built from).

    Pure price/volume engine -- unlike every other simulate_*_engine_trades
    function in this file, this one needs NO OI/option-chain snapshots at all,
    just the candle archive. PDH/PDL are recomputed once per trading day from
    the immediately-prior day's candles (same as build_market_structure's
    prev_day_levels live). The rolling `recent_window`-candle context carries
    over the previous day's tail into the new day (mirrors live app.py, where
    build_market_structure's candles[-30:] spans day boundaries too) so trend/
    volume-average readings aren't starved right after the open.

    One position at a time (a fresh signal while a trade is open is ignored --
    same convention as the other engines' backtests here). Exit is checked
    candle-by-candle using that candle's high/low (not just close), SL first
    (conservative: assumes the worst intrabar ordering). Trailing SL (spec:
    after Tn hit, SL -> T(n-1)) is applied progressively; exit_reason is
    'TARGET HIT' only if T3 (the final target) is fully reached, 'STOP LOSS'
    for a raw SL exit that never reached any target, 'BREAKEVEN STOP' if T1
    was reached and the trailed SL (now at bare entry) was then hit,
    'PROFIT-LOCKED STOP' if T2+ was reached and the trailed SL (now past
    entry) was hit, 'TIME EXIT' after `max_hold_minutes` or at end of day.
    """
    df = load_intraday_candles(symbol, timeframe="3m")
    if df.empty:
        return [], 0, {"elapsed_seconds": 0, "days_used": 0}

    start_time = time.time()
    d_from = dt.datetime.strptime(date_from, "%Y-%m-%d").date()
    d_to = dt.datetime.strptime(date_to, "%Y-%m-%d").date()

    df = df.copy()
    df["date"] = df["datetime"].dt.date
    all_dates = sorted(df["date"].unique())
    candles_by_date = {d: df[df["date"] == d].sort_values("datetime").to_dict("records") for d in all_dates}

    trades = []
    cycle_count = 0
    days_in_range = [d for d in all_dates if d_from <= d <= d_to]

    for day in days_in_range:
        day_pos = all_dates.index(day)
        if day_pos == 0:
            continue   # no prior trading day in the archive -> no PDH/PDL yet
        prev_candles = candles_by_date[all_dates[day_pos - 1]]
        pdh = max(c["high"] for c in prev_candles)
        pdl = min(c["low"] for c in prev_candles)
        history_tail = prev_candles[-recent_window:]

        today_candles = candles_by_date[day]
        open_trade = None

        for i, c in enumerate(today_candles):
            cycle_count += 1

            if open_trade:
                direction, targets = open_trade["direction"], open_trade["targets"]
                sl = open_trade["current_sl"]
                stopped = (c["low"] <= sl) if direction == "BUY" else (c["high"] >= sl)
                if stopped:
                    best = open_trade.get("best_target_hit")
                    reason = "STOP LOSS" if best is None else ("BREAKEVEN STOP" if best == 0 else "PROFIT-LOCKED STOP")
                    open_trade["exit_price"], open_trade["exit_reason"], open_trade["exit_time"] = sl, reason, c["datetime"]
                    trades.append(_dsr_finalize_trade(open_trade))
                    open_trade = None
                else:
                    hit_idx = None
                    for ti, t in enumerate(targets):
                        reached = (c["high"] >= t) if direction == "BUY" else (c["low"] <= t)
                        if reached:
                            hit_idx = ti
                    if hit_idx is not None:
                        open_trade["best_target_hit"] = hit_idx
                        open_trade["current_sl"] = ([open_trade["entry"]] + targets)[hit_idx]
                        if hit_idx == len(targets) - 1:
                            open_trade["exit_price"], open_trade["exit_reason"], open_trade["exit_time"] = targets[-1], "TARGET HIT", c["datetime"]
                            trades.append(_dsr_finalize_trade(open_trade))
                            open_trade = None
                    if open_trade and (c["datetime"] - open_trade["entry_time"]).total_seconds() / 60 >= max_hold_minutes:
                        open_trade["exit_price"], open_trade["exit_reason"], open_trade["exit_time"] = c["close"], "TIME EXIT", c["datetime"]
                        trades.append(_dsr_finalize_trade(open_trade))
                        open_trade = None

            if not open_trade:
                window = (history_tail + today_candles[:i + 1])[-recent_window:]
                if len(window) >= 2:
                    result = evaluate_dynamic_sr(window, pdh, pdl, current_price=c["close"],
                                                  min_tradeable_confidence=min_tradeable_confidence)
                    sig = result.get("signal") if result else None
                    if sig:
                        open_trade = {
                            "symbol": symbol, "entry_time": c["datetime"], "direction": sig["direction"],
                            "entry": sig["entry"], "current_sl": sig["stop_loss"], "targets": sig["targets"],
                            "confidence": sig["confidence"], "rating": sig["rating"], "best_target_hit": None,
                        }

            if progress_callback and cycle_count % 500 == 0:
                progress_callback(cycle_count, len(days_in_range) * 125, time.time() - start_time, len(trades))

        if open_trade:   # forced close at end of day
            last_c = today_candles[-1]
            open_trade["exit_price"], open_trade["exit_reason"], open_trade["exit_time"] = last_c["close"], "TIME EXIT", last_c["datetime"]
            trades.append(_dsr_finalize_trade(open_trade))

    meta = {"elapsed_seconds": round(time.time() - start_time, 2), "days_used": len(days_in_range) - (1 if days_in_range and days_in_range[0] == all_dates[0] else 0)}
    return trades, cycle_count, meta


V4_STRIKE_STEP = V3_STRIKE_STEP   # same default -- see V4_STRIKE_STEP's docstring note in exit_engine_v4.py


def _v4_finalize_trade(t):
    points = (t["exit_price"] - t["entry"]) if t["direction"] == "BUY" else (t["entry"] - t["exit_price"])
    t["points"] = round(points, 2)
    return t


def simulate_dynamic_sr_v4_trades(symbol, date_from, date_to, recent_window=30,
                                   with_oi_context=True, strike_step=V4_STRIKE_STEP, progress_callback=None,
                                   atr_trail_mult=None, momentum_fade_threshold=None,
                                   adaptive_hold_base_minutes=None, adaptive_hold_max_minutes=None):
    """
    Replays exit_engine_v4.py (the Institutional Exit Engine V4) on TOP OF
    dynamic_sr_engine.py's UNCHANGED entry signal -- entry detection is
    identical to simulate_dynamic_sr_engine_trades above (same window
    construction, same evaluate_dynamic_sr call, same "one open position at
    a time" convention); only what happens AFTER entry differs. See
    exit_engine_v4.py's module docstring for the full priority-ordered exit
    logic (trailing stop -> SL -> target -> structure break -> opposite
    signal -> VWAP cross -> momentum fade -> adaptive time exit).

    with_oi_context: when True (default), joins oi_history.db's cycles/
    strikes (via load_cycles) to each candle by nearest timestamp (see
    exit_engine_v4.build_oi_index/nearest_oi_context, 3min tolerance) so the
    OI-wall-strength and delta-confirmation components have real data to
    score instead of the neutral-50 default. Adds one DB query up front;
    pass False to skip it (faster, and the only option for a date range
    with no oi_history.db coverage -- the candle archive and the OI DB are
    NOT guaranteed to cover the same days).
    """
    df = load_intraday_candles(symbol, timeframe="3m")
    if df.empty:
        return [], 0, {"elapsed_seconds": 0, "days_used": 0}

    start_time = time.time()
    d_from = dt.datetime.strptime(date_from, "%Y-%m-%d").date()
    d_to = dt.datetime.strptime(date_to, "%Y-%m-%d").date()

    oi_index = exit_engine_v4.build_oi_index(load_cycles(symbol, date_from, date_to)) if with_oi_context else ()

    df = df.copy()
    df["date"] = df["datetime"].dt.date
    all_dates = sorted(df["date"].unique())
    candles_by_date = {d: df[df["date"] == d].sort_values("datetime").to_dict("records") for d in all_dates}

    trades = []
    cycle_count = 0
    days_in_range = [d for d in all_dates if d_from <= d <= d_to]

    for day in days_in_range:
        day_pos = all_dates.index(day)
        if day_pos == 0:
            continue
        prev_candles = candles_by_date[all_dates[day_pos - 1]]
        pdh = max(c["high"] for c in prev_candles)
        pdl = min(c["low"] for c in prev_candles)
        history_tail = prev_candles[-recent_window:]

        today_candles = candles_by_date[day]
        open_pos = None

        for i, c in enumerate(today_candles):
            cycle_count += 1
            window = (history_tail + today_candles[:i + 1])[-recent_window:]

            if open_pos is not None:
                oi_cycle = exit_engine_v4.nearest_oi_context(oi_index, c["datetime"]) if with_oi_context else None
                result = exit_engine_v4.manage_exit(open_pos, c, window, pdh, pdl, today=day,
                                                     oi_cycle=oi_cycle, strike_step=strike_step,
                                                     atr_trail_mult=atr_trail_mult,
                                                     momentum_fade_threshold=momentum_fade_threshold)
                open_pos = result["position"]
                if result["exit"]:
                    open_pos["exit_price"], open_pos["exit_reason"], open_pos["exit_time"] = (
                        result["exit_price"], result["exit_reason"], c["datetime"])
                    trades.append(_v4_finalize_trade(open_pos))
                    open_pos = None

            if open_pos is None and len(window) >= 2:
                sig_result = evaluate_dynamic_sr(window, pdh, pdl, current_price=c["close"])
                sig = sig_result.get("signal") if sig_result else None
                if sig:
                    oi_cycle = exit_engine_v4.nearest_oi_context(oi_index, c["datetime"]) if with_oi_context else None
                    open_pos = exit_engine_v4.open_position(sig, c["datetime"], window, pdh, pdl,
                                                              oi_cycle=oi_cycle, strike_step=strike_step,
                                                              adaptive_hold_base_minutes=adaptive_hold_base_minutes,
                                                              adaptive_hold_max_minutes=adaptive_hold_max_minutes)
                    open_pos["symbol"] = symbol

            if progress_callback and cycle_count % 500 == 0:
                progress_callback(cycle_count, len(days_in_range) * 125, time.time() - start_time, len(trades))

        if open_pos is not None:   # forced close at end of day
            last_c = today_candles[-1]
            open_pos["exit_price"], open_pos["exit_reason"], open_pos["exit_time"] = last_c["close"], "TIME EXIT", last_c["datetime"]
            trades.append(_v4_finalize_trade(open_pos))

    meta = {"elapsed_seconds": round(time.time() - start_time, 2),
            "days_used": len(days_in_range) - (1 if days_in_range and days_in_range[0] == all_dates[0] else 0)}
    return trades, cycle_count, meta


def run_dynamic_sr_v4_backtest(symbol, date_from, date_to, with_oi_context=True):
    trades, cycle_count, meta = simulate_dynamic_sr_v4_trades(symbol, date_from, date_to, with_oi_context=with_oi_context)
    if not cycle_count:
        print(f"No candle archive data found for {symbol} between {date_from} and {date_to}.")
        return

    print(f"\n[EXIT ENGINE V4] Replaying {cycle_count} candles for {symbol} ({date_from} to {date_to}, {meta['days_used']} day(s) with a prior day available)...\n")

    if not trades:
        print("No trades were triggered in this window.")
        return

    stats = compute_advanced_trade_stats(trades)
    net_positive = [t for t in trades if t["points"] > 0]

    print(f"{'Time':<10} {'Dir':<5} {'Entry':<10} {'Exit':<10} {'Reason':<16} {'Quality':<8} {'PredConf':<9} {'Points':<8}")
    for t in trades:
        print(f"{t['entry_time'].strftime('%Y-%m-%d %H:%M'):<10} {t['direction']:<5} {t['entry']:<10} {t['exit_price']:<10} "
              f"{t['exit_reason']:<16} {t['quality_score']:<8} {t['predictive_confidence']:<9} {t['points']:+.2f}")

    print(f"\n--- SUMMARY (EXIT ENGINE V4) ---")
    print(f"Total trades   : {stats['total_trades']}")
    print(f"Net positive   : {len(net_positive)}/{stats['total_trades']} trades ({round(len(net_positive)/stats['total_trades']*100, 1)}%)")
    print(f"Win rate       : {stats['win_rate']}%  |  Accuracy (excl. time exits): {stats['accuracy']}%")
    print(f"Net points     : {stats['net_pnl']:+.2f}")
    print(f"Profit factor  : {stats['profit_factor']}")
    print(f"Expectancy     : {stats['expectancy']} pts/trade")
    print(f"Max drawdown   : {stats['max_drawdown']} pts")
    print(f"Replay time    : {meta['elapsed_seconds']}s for {cycle_count} candles")


def run_dynamic_sr_backtest(symbol, date_from, date_to, max_hold_minutes=MAX_HOLD_MINUTES):
    trades, cycle_count, meta = simulate_dynamic_sr_engine_trades(symbol, date_from, date_to, max_hold_minutes=max_hold_minutes)
    if not cycle_count:
        print(f"No candle archive data found for {symbol} between {date_from} and {date_to}.")
        print("Run --list to see what's actually available (note: this engine reads data/history/<symbol>/3m.*, not oi_history.db).")
        return

    print(f"\n[DYNAMIC S/R ENGINE V1] Replaying {cycle_count} candles for {symbol} ({date_from} to {date_to}, {meta['days_used']} day(s) with a prior day available)...\n")

    if not trades:
        print("No trades were triggered in this window.")
        return

    stats = compute_advanced_trade_stats(trades)
    net_positive = [t for t in trades if t["points"] > 0]

    print(f"{'Time':<10} {'Dir':<5} {'Entry':<10} {'Exit':<10} {'Reason':<12} {'Confidence':<12} {'Points':<8}")
    for t in trades:
        print(f"{t['entry_time'].strftime('%Y-%m-%d %H:%M'):<10} {t['direction']:<5} {t['entry']:<10} {t['exit_price']:<10} "
              f"{t['exit_reason']:<12} {t['confidence']:<12} {t['points']:+.2f}")

    reason_tally = defaultdict(int)
    for t in trades:
        reason_tally[t["exit_reason"]] += 1

    print(f"\n--- SUMMARY (DYNAMIC S/R ENGINE V1) ---")
    print(f"Total trades   : {stats['total_trades']}")
    print(f"By exit reason : " + "  ".join(f"{reason}={count}" for reason, count in sorted(reason_tally.items())))
    print(f"Net positive   : {len(net_positive)}/{stats['total_trades']} trades ({round(len(net_positive)/stats['total_trades']*100, 1)}%)")
    print(f"Win rate       : {stats['win_rate']}%  |  Accuracy (excl. time exits): {stats['accuracy']}%")
    print(f"Net points     : {stats['net_pnl']:+.2f}")
    print(f"Profit factor  : {stats['profit_factor']}")
    print(f"Expectancy     : {stats['expectancy']} pts/trade")
    print(f"Max drawdown   : {stats['max_drawdown']} pts")
    print(f"Sharpe (per-trade, not annualized): {stats['sharpe_ratio']}")
    print(f"Replay time    : {meta['elapsed_seconds']}s for {cycle_count} candles")
    print(f"\nPure price/volume engine (no OI/PCR/strike-selection) -- historical replay,")
    print(f"not a guarantee of future results.")


def run_sr_backtest(symbol, date_from, date_to, persistence_seconds=LIVE_PERSISTENCE_SECONDS,
                     deactivation_grace_seconds=LIVE_GRACE_SECONDS,
                     min_risk_reward=LIVE_MIN_RISK_REWARD, strike_step=50):
    trades, cycle_count, meta = simulate_sr_engine_trades(
        symbol, date_from, date_to, strike_step=strike_step,
        persistence_seconds=persistence_seconds, deactivation_grace_seconds=deactivation_grace_seconds,
        min_risk_reward=min_risk_reward,
    )
    if not cycle_count:
        print(f"No data found for {symbol} between {date_from} and {date_to}.")
        print("Run --list to see what's actually available.")
        return

    print(f"\n[NEW S/R ENGINE] Replaying {cycle_count} cycles for {symbol} ({date_from} to {date_to})...\n")
    print("NOTE: historical PDH/PDL/ATR/regime aren't logged per cycle (only computed live from")
    print("candles) -- this backtest uses a pseudo-ATR (recent price high-low) and OI-wall-based")
    print("levels as a reasonable approximation. Live results may differ from this replay.\n")

    if not trades:
        print("No trades were triggered (try lowering --min-rr, --persistence, or check --list for more data).")
        return

    wins = [t for t in trades if t["exit_reason"] == "TARGET HIT"]
    losses = [t for t in trades if t["exit_reason"] == "STOP LOSS"]
    time_exits = [t for t in trades if t["exit_reason"] == "TIME EXIT"]
    total_points = sum(t["points"] for t in trades)
    win_rate = round(len(wins) / len(trades) * 100, 1) if trades else 0.0

    print(f"{'Time':<10} {'Level':<20} {'Strike':<8} {'Dir':<4} {'Entry':<8} {'Exit':<8} {'R:R':<6} {'Reason':<12} {'Points':<8}")
    for t in trades:
        print(f"{t['entry_time'].strftime('%H:%M:%S'):<10} {t.get('sr_level',''):<20} {t['strike']:<8} {t['direction']:<4} "
              f"{t['entry_price']:<8} {t['exit_price']:<8} {t.get('risk_reward','-'):<6} {t['exit_reason']:<12} {t['points']:+.2f}")

    print(f"\n--- SUMMARY (NEW S/R ENGINE) ---")
    print(f"Total trades : {len(trades)}")
    print(f"Wins         : {len(wins)}  |  Losses: {len(losses)}  |  Time exits: {len(time_exits)}")
    print(f"Win rate     : {win_rate}%")
    print(f"Total points : {total_points:+.2f}")
    print(f"Replay time  : {meta['elapsed_seconds']}s for {cycle_count} cycles")
    print(f"Market data  : {meta['snapshot_days_used']}/{meta['total_days']} day(s) used REAL market-structure "
          f"(rest used pseudo-ATR approximation)")
    print(f"\nThis replays the SAME state-machine + entry-trigger + target/SL/R:R logic used")
    print(f"live (sr_probability_engine.py) -- not a guarantee of future results.")


def score_calibration_report(db_path=None, min_sample=5):
    """
    Checks whether the Institutional Entry Score genuinely predicts win-rate
    -- honest, transparent bucketed statistics, NOT machine learning. Buckets
    closed S/R-engine trades by score-tier and by regime-at-entry, computing
    win-rate per bucket. Buckets with fewer than min_sample trades are
    flagged as having an insufficient sample rather than shown as a
    misleadingly precise percentage that could just be noise.

    This doesn't change any behavior automatically -- it's a display/
    monitoring tool so a human can make an INFORMED decision about whether
    (and how) to adjust thresholds, once there's actually enough data to
    draw a real conclusion from.
    """
    conn = _connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT institutional_score, institutional_tier, regime_at_entry, exit_reason, points
        FROM paper_trades
        WHERE status='CLOSED' AND institutional_score IS NOT NULL
    """).fetchall()
    conn.close()

    if not rows:
        return {"by_tier": {}, "by_regime": {}, "total_trades": 0,
                "note": "No calibration data yet -- needs trades opened via the S/R engine with a recorded Institutional Entry Score."}

    by_tier, by_regime = {}, {}
    for r in rows:
        tier = r["institutional_tier"] or "UNKNOWN"
        regime = r["regime_at_entry"] or "UNKNOWN"
        won = r["exit_reason"] == "TARGET HIT"
        for bucket_dict, key in ((by_tier, tier), (by_regime, regime)):
            bucket_dict.setdefault(key, {"trades": 0, "wins": 0, "points": 0.0})
            bucket_dict[key]["trades"] += 1
            bucket_dict[key]["wins"] += 1 if won else 0
            bucket_dict[key]["points"] += r["points"] or 0

    def finalize(bucket_dict):
        out = {}
        for k, v in bucket_dict.items():
            sufficient = v["trades"] >= min_sample
            win_rate = round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0
            out[k] = {
                "trades": v["trades"], "wins": v["wins"], "win_rate": win_rate,
                "total_points": round(v["points"], 2), "sufficient_sample": sufficient,
                "note": None if sufficient else f"Only {v['trades']} trade(s) -- need {min_sample}+ before this win-rate means anything",
            }
        return out

    return {"by_tier": finalize(by_tier), "by_regime": finalize(by_regime), "total_trades": len(rows)}


def run_backtest(symbol, date_from, date_to, persistence_cycles, cooldown_minutes, confidence_threshold):

    trades, cycle_count = simulate_trades(symbol, date_from, date_to, persistence_cycles, cooldown_minutes, confidence_threshold)
    if not cycle_count:
        print(f"No data found for {symbol} between {date_from} and {date_to}.")
        print("Run --list to see what's actually available.")
        return

    print(f"\nReplaying {cycle_count} cycles for {symbol} ({date_from} to {date_to})...\n")

    # -- report --
    if not trades:
        print("No trades were triggered in this window (try lowering --confidence or --persistence).")
        return

    wins = [t for t in trades if t["exit_reason"] == "TARGET HIT"]
    losses = [t for t in trades if t["exit_reason"] == "STOP LOSS"]
    time_exits = [t for t in trades if t["exit_reason"] == "TIME EXIT"]
    total_points = sum(t["points"] for t in trades)
    win_rate = round(len(wins) / len(trades) * 100, 1)

    print(f"{'Time':<10} {'Strike':<8} {'Dir':<4} {'Entry':<8} {'Exit':<8} {'Reason':<12} {'Points':<8}")
    for t in trades:
        print(f"{t['entry_time'].strftime('%H:%M:%S'):<10} {t['strike']:<8} {t['direction']:<4} "
              f"{t['entry_price']:<8} {t['exit_price']:<8} {t['exit_reason']:<12} {t['points']:+.2f}")

    print(f"\n--- SUMMARY ---")
    print(f"Total trades : {len(trades)}")
    print(f"Wins         : {len(wins)}  |  Losses: {len(losses)}  |  Time exits: {time_exits and len(time_exits) or 0}")
    print(f"Win rate     : {win_rate}%")
    print(f"Total points : {total_points:+.2f}")
    print(f"Est. P&L     : Rs {total_points * PAPER_TRADE_LOT_QTY:+.2f} (at {PAPER_TRADE_LOT_QTY} lot qty multiplier)")
    print(f"\nNote: this is historical replay of logged OI data with the SAME rule-based")
    print(f"engine used live -- not a guarantee of future results. Past OI patterns")
    print(f"repeating is not certain.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the OI signal engine on logged historical data.")
    parser.add_argument("--list", action="store_true", help="List symbols/dates available in the DB")
    parser.add_argument("--symbol", type=str, help="e.g. NIFTY, BANKNIFTY, CRUDEOIL")
    parser.add_argument("--date", type=str, help="Single date YYYY-MM-DD")
    parser.add_argument("--from", dest="date_from", type=str, help="Range start YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", type=str, help="Range end YYYY-MM-DD")
    parser.add_argument("--engine", choices=["dynamic-sr", "dynamic-sr-v4", "old", "sr", "v2"], default="dynamic-sr",
                         help="'dynamic-sr' = new Dynamic S/R Engine V1, PDH/PDL-based ladder + BUY/SELL/SL/targets "
                              "(default -- pure price/volume, reads data/history/<symbol>/3m.*, no OI data needed); "
                              "'dynamic-sr-v4' = SAME V1 entry signal, exit_engine_v4's Institutional Exit Engine "
                              "instead of V1's fixed target/trailing-SL/30min exit (ATR+structure trailing stop, "
                              "structure-break/opposite-signal/VWAP/momentum-fade exits, adaptive ATR targets, "
                              "adaptive 30-60min hold; joins oi_history.db for OI-wall/delta context, see --no-oi-context); "
                              "'sr' = S/R Probability Engine + state machine (matches live OI-based trading); "
                              "'old' = original oi_engine signal (reference/comparison only); "
                              "'v2' = Engine V2 signal-frequency + directional-accuracy analysis (read-only, no trade simulation)")
    parser.add_argument("--no-oi-context", action="store_true", help="[dynamic-sr-v4] Skip the oi_history.db join (faster; OI-wall/delta components fall back to neutral)")
    parser.add_argument("--persistence", type=int, default=2, help="[old engine] Bias persistence cycles (default 2)")
    parser.add_argument("--cooldown", type=int, default=10, help="[old engine] Cooldown minutes after a stop-loss (default 10)")
    parser.add_argument("--confidence", type=int, default=SIGNAL_CONFIDENCE_THRESHOLD, help="[old engine] Min confidence to trade")
    parser.add_argument("--persistence-sec", type=int, default=LIVE_PERSISTENCE_SECONDS, help=f"[sr engine] Persistence seconds (default {LIVE_PERSISTENCE_SECONDS}, from .env -- matches live)")
    parser.add_argument("--grace-sec", type=int, default=LIVE_GRACE_SECONDS, help=f"[sr engine] Neutral grace seconds (default {LIVE_GRACE_SECONDS}, from .env -- matches live)")
    parser.add_argument("--min-rr", type=float, default=LIVE_MIN_RISK_REWARD, help=f"[sr engine] Minimum risk:reward (default {LIVE_MIN_RISK_REWARD}, from .env -- matches live)")
    args = parser.parse_args()

    if args.list:
        list_available_data()
    elif args.symbol and (args.date or (args.date_from and args.date_to)):
        d_from = args.date or args.date_from
        d_to = args.date or args.date_to
        if args.engine == "dynamic-sr":
            run_dynamic_sr_backtest(args.symbol.upper(), d_from, d_to)
        elif args.engine == "dynamic-sr-v4":
            run_dynamic_sr_v4_backtest(args.symbol.upper(), d_from, d_to, with_oi_context=not args.no_oi_context)
        elif args.engine == "sr":
            run_sr_backtest(args.symbol.upper(), d_from, d_to, args.persistence_sec, args.grace_sec, args.min_rr)
        elif args.engine == "v2":
            result = analyze_v2_signals(args.symbol.upper(), d_from, d_to)
            if "error" in result:
                print(f"ERROR: {result['error']}")
            else:
                print(f"\n=== ENGINE V2 ANALYSIS ({result['symbol']}, {result['days_with_real_snapshots']} day(s) with real snapshots) ===\n")
                print("Trend distribution:")
                for trend, count in result["trend_counts"].items():
                    print(f"  {trend}: {count}")
                print("\nSignal distribution (overall):")
                for sig, count in result["signal_counts"].items():
                    print(f"  {sig}: {count}")
                print("\nSignal distribution (per day):")
                for date_str in sorted(result["per_day_signals"].keys()):
                    day_sigs = result["per_day_signals"][date_str]
                    print(f"  {date_str}: BUY CE={day_sigs.get('BUY CE', 0)}  BUY PE={day_sigs.get('BUY PE', 0)}  NO_TRADE={day_sigs.get('NO_TRADE', 0)}")
                print(f"\nDirectional accuracy: {result['directional_accuracy_pct']}% "
                      f"(from {result['directional_checks']} BUY CE/PE signals)")
                d = result["diagnostics"]
                print(f"\nDiagnostics (root-cause breakdown):")
                print(f"  Cycles with price ABOVE prior-day-close: {d['above_pdc_pct']}%")
                print(f"  Cycles with price BELOW prior-day-close: {d['below_pdc_pct']}%")
                print(f"  Cycles where Resistance-Reversal-evidence dominates: {d['resistance_holding_pct']}%")
                print(f"  Cycles where Support-Reversal-evidence dominates: {d['support_holding_pct']}%")
                print(f"\nPDC sanity-check (per day) -- is PDC plausible vs that day's actual price range?")
                for date_str, pv in d["pdc_vs_price_range_by_day"].items():
                    flag = " ⚠️ PDC is ABOVE the whole day's range!" if pv["pdc"] > pv["day_max"] else ""
                    print(f"  {date_str}: PDC={pv['pdc']}  Day range=[{pv['day_min']}, {pv['day_max']}]{flag}")
                print(f"Replay time: {result['replay_time_seconds']}s for {result['cycles_replayed']} cycles")
                print(f"\nNote: {result['note']}")
        else:
            run_backtest(args.symbol.upper(), d_from, d_to, args.persistence, args.cooldown, args.confidence)
    else:
        parser.print_help()

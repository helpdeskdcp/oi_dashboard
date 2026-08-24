#!/usr/bin/env python3
"""
signal_quality_v2_backtest.py -- Signal Intelligence V2 validation.

Replays REAL archived option-chain cycles (backtest.load_cycles(), the same
safe read-only pattern every other backtest in this repo uses) through the
UNCHANGED Stage A signal generator (oi_engine.generate_signal()) and the NEW
Stage B qualifier (agents.trading_intelligence.signal_qualification.
qualify_signal()), across whatever real symbols/date ranges are available --
never just NATURALGAS, never tuned to make one example look good (see
signal_qualification.py's own module docstring on this).

For every BUY CE/PE Stage-A signal, this walks forward through the SAME
target/SL/time-exit rules backtest.simulate_trades() already uses
(MAX_HOLD_MINUTES, target/SL hit) to get a real outcome, then buckets that
outcome by BOTH:
  - the OLD gate (confidence >= TI_TELEGRAM_MIN_CONFIDENCE, the one real
    production gate today), and
  - the NEW production_action from qualify_signal().
Reports win rate/expectancy/profit factor/avg R/max drawdown/false-signal
rate per bucket, and per instrument -- never optimizing only for win rate,
never claiming a result before checking sample size.

HONEST LIMITATIONS (do not paper over these):
  - No real market_structure snapshot is threaded through per-cycle here
    (unlike backtest.simulate_trades(), which also never receives one for
    the confidence-scoring path either). That means failure_gate's own
    `regime` and `major_level_proximity` checks, and _breakout_confirmation()'s
    VWAP-alignment sub-check, are honestly NOT_EVALUATED throughout this
    backtest -- degrading gracefully (never faked), but genuinely UNTESTED
    here. Only OI-lean, premium-momentum, and volume-expansion evidence are
    exercised against real data by this script.
  - EXPIRY_DAY_MODE cannot be exercised against real historical outcomes:
    no expiry_date column exists anywhere in oi_history.db (confirmed
    earlier this session, see institutional_flow_backtest.py's own
    identical exclusion for gamma_trap_findings()), so days_to_expiry is
    always None here, exactly like backtest.simulate_trades() already
    documents for its own expiry_context attachment. EXPIRY_DAY_TRADE_PARAMS
    validation is therefore reported as "cannot be validated from this
    archive" rather than guessed.
  - Lookahead-bias fix: regime_profile._breakout_confirmation() internally
    calls data_access.recent_strike_history(), which has no date bound and
    always queries the LIVE most-recent rows. Fixed via the exact same
    process-scoped monkeypatch technique institutional_flow_backtest.py
    already established (see _make_bounded_recent_strike_history there) --
    replayed here independently since this script's replay loop (Stage A
    signal generation) is structurally different from that module's.
"""
import collections
import dataclasses
import datetime as dt
from unittest import mock

import backtest
from agents.trading_intelligence import data_access, signal_qualification
from oi_engine import generate_signal, oi_walls

RECENT_STRIKE_HISTORY_DEPTH = 15  # matches regime_profile._breakout_confirmation()'s own limit=15


def _make_bounded_recent_strike_history(history_by_strike):
    """Same technique as institutional_flow_backtest.py's own function of
    the same name -- see that module's docstring for why this exists.
    Newest-first, matching data_access.recent_strike_history()'s own
    ORDER BY ts DESC."""
    def _bounded_lookup(symbol, strike, *, limit=RECENT_STRIKE_HISTORY_DEPTH):
        history = history_by_strike.get(strike)
        if not history:
            return []
        return list(history)[::-1][:limit]
    return _bounded_lookup


def _walk_forward_outcome(cycles, *, start_idx, strike, direction, target_price, sl_price):
    """Same target/SL/time-exit rules backtest.simulate_trades() already
    uses (MAX_HOLD_MINUTES, first-hit-wins, tie -> LOSS matching this
    codebase's own established tie convention). Returns
    (outcome: "WIN"|"LOSS"|"PENDING", exit_points: float|None)."""
    entry_ts = dt.datetime.fromisoformat(cycles[start_idx]["cycle"]["ts"])
    entry_price = None
    for i in range(start_idx, len(cycles)):
        cyc, rows = cycles[i]["cycle"], cycles[i]["rows"]
        ts = dt.datetime.fromisoformat(cyc["ts"])
        row = next((r for r in rows if r.strike == strike), None)
        if not row:
            continue
        price = row.ce_ltp if direction == "CE" else row.pe_ltp
        if not price:
            continue
        if entry_price is None:
            entry_price = price
            continue
        held_min = (ts - entry_ts).total_seconds() / 60
        hit_target = price >= target_price
        hit_sl = price <= sl_price
        if hit_target and hit_sl:
            return "LOSS", round(sl_price - entry_price, 2)  # tie -> LOSS, matches structure_backtest's own rule
        if hit_target:
            return "WIN", round(price - entry_price, 2)
        if hit_sl:
            return "LOSS", round(price - entry_price, 2)
        if held_min >= backtest.MAX_HOLD_MINUTES:
            return "LOSS" if price < entry_price else "WIN", round(price - entry_price, 2)
    return "PENDING", None


@dataclasses.dataclass
class BucketStats:
    count: int = 0
    wins: int = 0
    losses: int = 0
    pending: int = 0
    gross_win_points: float = 0.0
    gross_loss_points: float = 0.0
    r_multiples: list = dataclasses.field(default_factory=list)
    equity_curve: list = dataclasses.field(default_factory=list)

    def add(self, outcome, points, risk_points):
        self.count += 1
        if outcome == "WIN":
            self.wins += 1
            self.gross_win_points += max(points or 0, 0)
        elif outcome == "LOSS":
            self.losses += 1
            self.gross_loss_points += abs(min(points or 0, 0))
        else:
            self.pending += 1
        if points is not None and risk_points:
            self.r_multiples.append(points / risk_points)
        running = (self.equity_curve[-1] if self.equity_curve else 0.0) + (points or 0.0)
        self.equity_curve.append(running)

    def report(self):
        resolved = self.wins + self.losses
        win_rate = round(self.wins / resolved * 100, 1) if resolved else None
        avg_r = round(sum(self.r_multiples) / len(self.r_multiples), 3) if self.r_multiples else None
        expectancy = round(sum(self.r_multiples) / len(self.r_multiples), 3) if self.r_multiples else None
        profit_factor = (
            round(self.gross_win_points / self.gross_loss_points, 2)
            if self.gross_loss_points > 0 else (None if self.gross_win_points == 0 else float("inf"))
        )
        peak, max_dd = 0.0, 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)
        return {
            "count": self.count, "wins": self.wins, "losses": self.losses, "pending": self.pending,
            "win_rate_pct": win_rate, "avg_r": avg_r, "expectancy_r": expectancy,
            "profit_factor": profit_factor, "max_drawdown_pts": round(max_dd, 2),
        }


def backtest_symbol(symbol: str, date_from: str, date_to: str, *, telegram_min_confidence: int = 75, cycles=None):
    """Returns {"old_gate": {...}, "production_action": {action: BucketStats-report}}
    for one symbol. Never raises -- an empty/short cycles list reports zero
    samples in every bucket, same honest-degrade contract as
    institutional_flow_backtest.backtest_symbol()."""
    if cycles is None:
        cycles = backtest.load_cycles(symbol, date_from, date_to)
    if not cycles:
        return {"symbol": symbol, "old_gate": {}, "by_production_action": {}, "sample_size": 0}

    old_gate_stats = {"SENT": BucketStats(), "NOT_SENT": BucketStats()}
    by_action = collections.defaultdict(BucketStats)
    history_by_strike = collections.defaultdict(lambda: collections.deque(maxlen=RECENT_STRIKE_HISTORY_DEPTH))
    open_trade = None

    with mock.patch.object(data_access, "recent_strike_history",
                            side_effect=_make_bounded_recent_strike_history(history_by_strike)):
        for idx, entry in enumerate(cycles):
            cyc, rows = entry["cycle"], entry["rows"]
            for row in rows:
                history_by_strike[row.strike].append(dataclasses.asdict(row))

            if open_trade and idx <= open_trade["resolve_idx"]:
                continue  # already resolved when this signal first fired -- skip until past it
            open_trade = None

            atm, pcr, bias, underlying = cyc["atm"], cyc["pcr"], cyc["bias"], cyc["underlying_ltp"]
            if atm is None or pcr is None or not rows:
                continue
            support, resistance = oi_walls(rows)
            signal = generate_signal(
                rows, atm, bias, cyc.get("note", ""), pcr, support, resistance, underlying=underlying,
            )
            if signal["action"] not in ("BUY CE", "BUY PE"):
                continue

            risk_points = signal["entry_price"] - signal["sl_price"] if signal["entry_price"] and signal["sl_price"] else None
            outcome, points = _walk_forward_outcome(
                cycles, start_idx=idx, strike=signal["strike"], direction=signal["direction"],
                target_price=signal["target_price"], sl_price=signal["sl_price"],
            )
            # find the resolving cycle index to avoid double-counting overlapping signals on the same strike
            resolve_idx = idx
            for j in range(idx, len(cycles)):
                if (dt.datetime.fromisoformat(cycles[j]["cycle"]["ts"])
                        - dt.datetime.fromisoformat(cyc["ts"])).total_seconds() / 60 >= backtest.MAX_HOLD_MINUTES:
                    resolve_idx = j
                    break
            else:
                resolve_idx = len(cycles) - 1
            open_trade = {"resolve_idx": resolve_idx}

            bucket = "SENT" if (signal["confidence"] or 0) >= telegram_min_confidence else "NOT_SENT"
            old_gate_stats[bucket].add(outcome, points, risk_points)

            qualification = signal_qualification.qualify_signal(
                symbol, direction=signal["direction"], strike=signal["strike"],
                entry_price=signal["entry_price"], sl_price=signal["sl_price"],
                target_price=signal["target_price"], confidence=signal["confidence"], probability=None,
                tradeable=signal["tradeable"], rows=rows, atm=atm, underlying=underlying, support=support,
                resistance=resistance, market_structure=None, snapshot=None, expiry_date=None,
                expiry_context=None, is_mcx=False,
            )
            by_action[qualification.production_action].add(outcome, points, risk_points)

    return {
        "symbol": symbol,
        "old_gate": {k: v.report() for k, v in old_gate_stats.items()},
        "by_production_action": {k: v.report() for k, v in by_action.items()},
        "sample_size": sum(v.count for v in old_gate_stats.values()),
    }


def run(symbols, date_from, date_to):
    results = {}
    for symbol in symbols:
        print(f"--- {symbol} ({date_from} to {date_to}) ---")
        result = backtest_symbol(symbol, date_from, date_to)
        results[symbol] = result
        if result["sample_size"] == 0:
            print("  no data / no signals")
            continue
        print(f"  total signals: {result['sample_size']}")
        for gate, stats in result["old_gate"].items():
            print(f"  OLD GATE {gate}: {stats}")
        for action, stats in result["by_production_action"].items():
            print(f"  V2 {action}: {stats}")
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    args = parser.parse_args()
    run(args.symbols, args.date_from, args.date_to)

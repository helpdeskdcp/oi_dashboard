"""
agents/trading_intelligence/structure_tuning.py -- Milestone 20, Phase 7:
the bounded, rate-limited, fully-audited autonomous half of the
adaptive learning loop. structure_backtest.py answers "how would
detect_role_reversal()'s tunable parameters have performed
historically"; this module is the only thing that ever ACTS on that
answer -- and only within hard guardrails, never a free-running
optimizer:

1. HARD BOUNDS (TUNABLE_PARAMS[...]["bounds"]) -- a candidate value
   outside the safe range is never even considered, regardless of its
   backtested win rate.
2. MINIMUM SAMPLE SIZE (TUNING_MIN_SAMPLE_SIZE) -- never acts on thin
   evidence, the same "don't trust a stat below this sample size" gate
   every calibration surface in this project already uses.
3. MINIMUM IMPROVEMENT MARGIN (TUNING_MIN_IMPROVEMENT) -- a candidate
   must beat the CURRENT live value by a real margin, not just edge it
   out by backtest noise.
4. COOLDOWN (TUNING_COOLDOWN_DAYS) -- a parameter that was just changed
   cannot be changed again for a full cooldown window, so this can
   never thrash a parameter back and forth cycle to cycle.
5. FULL AUDIT LOG (structure_tuning_log, this module's own table) --
   EVERY evaluation this module ever runs is recorded, whether or not
   it resulted in a change, with the complete backtest evidence behind
   the decision. "Autonomous" here means "acts without requiring a
   human to approve each individual change," never "acts invisibly."

Still confined to the exact same scope every other Milestone 20 module
holds to: this only ever mutates institutional_levels.MAX_RETEST_CANDLES/
MIN_VOLUME_MULTIPLIER (the structure-ALERT detector's own thresholds).
It never touches paper_trading.py, ai_trading_engine.py, position
sizing, or any order-placement path -- there is no code path from here
to a real or paper trade.
"""
import dataclasses
import datetime as dt
import logging
import sqlite3

import institutional_levels as il

from .. import config
from . import structure_backtest

log = logging.getLogger(__name__)

DB_PATH = "oi_history.db"

TUNING_MIN_SAMPLE_SIZE = 30
TUNING_MIN_IMPROVEMENT = 0.05   # a candidate must beat the current live value's win rate by >= 5 percentage points
TUNING_COOLDOWN_DAYS = 7        # a parameter just changed can't change again for this long
TUNING_EVALUATION_INTERVAL_HOURS = 24   # how often evaluate_and_maybe_tune() does any work at all

TUNABLE_PARAMS = {
    "max_retest_candles": {"bounds": (2, 5), "candidates": (2, 3, 4, 5), "attr": "MAX_RETEST_CANDLES"},
    "min_volume_multiplier": {"bounds": (1.0, 2.0), "candidates": (1.0, 1.2, 1.5, 1.8, 2.0), "attr": "MIN_VOLUME_MULTIPLIER"},
}


@dataclasses.dataclass
class TuningDecision:
    parameter: str
    current_value: float
    best_candidate: float | None
    current_win_rate: float | None
    best_win_rate: float | None
    sample_size: int
    applied: bool
    reason: str


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS structure_tuning_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, parameter TEXT NOT NULL,
                current_value REAL, best_candidate REAL, current_win_rate REAL, best_win_rate REAL,
                sample_size INTEGER, applied INTEGER NOT NULL, reason TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_structure_tuning_log_parameter_ts ON structure_tuning_log(parameter, ts)")
        conn.commit()
    finally:
        conn.close()


def _record(decision: TuningDecision, *, now: dt.datetime) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO structure_tuning_log (ts, parameter, current_value, best_candidate, current_win_rate, "
            "best_win_rate, sample_size, applied, reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (now.isoformat(), decision.parameter, decision.current_value, decision.best_candidate,
             decision.current_win_rate, decision.best_win_rate, decision.sample_size,
             int(decision.applied), decision.reason),
        )
        conn.commit()
    finally:
        conn.close()


def list_tuning_history(*, parameter: str | None = None, limit: int = 50) -> list:
    conn = _connect()
    try:
        if parameter:
            rows = conn.execute(
                "SELECT * FROM structure_tuning_log WHERE parameter=? ORDER BY ts DESC LIMIT ?", (parameter, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM structure_tuning_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _last_evaluation_ts() -> dt.datetime | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT ts FROM structure_tuning_log ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return dt.datetime.fromisoformat(row["ts"]) if row else None


def _last_applied_ts(parameter: str) -> dt.datetime | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT ts FROM structure_tuning_log WHERE parameter=? AND applied=1 ORDER BY ts DESC LIMIT 1",
            (parameter,),
        ).fetchone()
    finally:
        conn.close()
    return dt.datetime.fromisoformat(row["ts"]) if row else None


def _aggregate_win_rate(symbols: list, *, max_retest_candles: int, min_volume_multiplier: float) -> tuple:
    """Sums wins/losses for this parameter combination across every
    symbol's own real candle archive. Returns (win_rate: float|None,
    sample_size: int) -- win_rate is None below TUNING_MIN_SAMPLE_SIZE,
    same honest-degrade contract as every calibration surface here."""
    total_wins = total_losses = 0
    for symbol in symbols:
        result = structure_backtest.backtest_parameters(
            symbol, structure_backtest.data_access.load_candles(symbol).to_dict("records"),
            max_retest_candles=max_retest_candles, min_volume_multiplier=min_volume_multiplier,
        )
        total_wins += result.wins
        total_losses += result.losses
    sample_size = total_wins + total_losses
    win_rate = round(total_wins / sample_size, 4) if sample_size >= TUNING_MIN_SAMPLE_SIZE else None
    return win_rate, sample_size


def _evaluate_one_parameter(parameter: str, symbols: list, *, apply: bool, now: dt.datetime) -> TuningDecision:
    spec = TUNABLE_PARAMS[parameter]
    current_value = getattr(il, spec["attr"])

    last_applied = _last_applied_ts(parameter)
    if last_applied is not None and (now - last_applied).days < TUNING_COOLDOWN_DAYS:
        decision = TuningDecision(parameter=parameter, current_value=current_value, best_candidate=None,
                                   current_win_rate=None, best_win_rate=None, sample_size=0, applied=False,
                                   reason=f"cooldown active -- last changed {last_applied.isoformat()}")
        _record(decision, now=now)
        return decision

    current_win_rate, current_sample = _aggregate_win_rate(
        symbols, max_retest_candles=(current_value if parameter == "max_retest_candles" else il.MAX_RETEST_CANDLES),
        min_volume_multiplier=(current_value if parameter == "min_volume_multiplier" else il.MIN_VOLUME_MULTIPLIER),
    )

    best_candidate, best_win_rate, best_sample = None, None, 0
    for candidate in spec["candidates"]:
        if candidate == current_value:
            continue
        lo, hi = spec["bounds"]
        if not (lo <= candidate <= hi):
            continue
        win_rate, sample = _aggregate_win_rate(
            symbols,
            max_retest_candles=(candidate if parameter == "max_retest_candles" else il.MAX_RETEST_CANDLES),
            min_volume_multiplier=(candidate if parameter == "min_volume_multiplier" else il.MIN_VOLUME_MULTIPLIER),
        )
        if win_rate is not None and (best_win_rate is None or win_rate > best_win_rate):
            best_candidate, best_win_rate, best_sample = candidate, win_rate, sample

    if current_win_rate is None or best_win_rate is None:
        decision = TuningDecision(parameter=parameter, current_value=current_value, best_candidate=best_candidate,
                                   current_win_rate=current_win_rate, best_win_rate=best_win_rate,
                                   sample_size=max(current_sample, best_sample), applied=False,
                                   reason="insufficient sample size for a confident comparison")
        _record(decision, now=now)
        return decision

    if best_win_rate - current_win_rate < TUNING_MIN_IMPROVEMENT:
        decision = TuningDecision(parameter=parameter, current_value=current_value, best_candidate=best_candidate,
                                   current_win_rate=current_win_rate, best_win_rate=best_win_rate,
                                   sample_size=best_sample, applied=False,
                                   reason=f"best alternative ({best_win_rate:.1%}) doesn't beat current "
                                          f"({current_win_rate:.1%}) by >= {TUNING_MIN_IMPROVEMENT:.0%}")
        _record(decision, now=now)
        return decision

    applied = False
    reason = f"applied: {best_win_rate:.1%} beats current {current_win_rate:.1%} by >= {TUNING_MIN_IMPROVEMENT:.0%}"
    if apply:
        setattr(il, spec["attr"], best_candidate)
        applied = True
        log.info(f"[STRUCTURE_TUNING] {parameter}: {current_value} -> {best_candidate} "
                 f"(win rate {current_win_rate:.1%} -> {best_win_rate:.1%}, sample={best_sample})")
    else:
        reason = f"dry-run (not applied): {reason}"
    decision = TuningDecision(parameter=parameter, current_value=current_value, best_candidate=best_candidate,
                               current_win_rate=current_win_rate, best_win_rate=best_win_rate,
                               sample_size=best_sample, applied=applied, reason=reason)
    _record(decision, now=now)
    return decision


def evaluate_and_maybe_tune(symbols: list, *, apply: bool = True, force: bool = False,
                             now: dt.datetime | None = None) -> dict:
    """The full evaluation pass -- one TuningDecision per TUNABLE_PARAMS
    entry, each independently gated by its own cooldown. The whole pass
    itself only runs at most once per TUNING_EVALUATION_INTERVAL_HOURS
    (checked against the most recent log row of ANY parameter) unless
    `force=True` -- keeps the (real, ~seconds-per-symbol) backtest cost
    off the hot path of every single live cycle. `apply=False` (used by
    the CLI's --dry-run) computes and logs the same decisions without
    ever mutating institutional_levels' live constants."""
    now = now or dt.datetime.now()
    if not force:
        last_eval = _last_evaluation_ts()
        if last_eval is not None and (now - last_eval).total_seconds() < TUNING_EVALUATION_INTERVAL_HOURS * 3600:
            return {"ran": False, "reason": f"last evaluation was {last_eval.isoformat()} -- "
                                             f"next due after {TUNING_EVALUATION_INTERVAL_HOURS}h"}

    decisions = [_evaluate_one_parameter(p, symbols, apply=apply, now=now) for p in TUNABLE_PARAMS]
    return {"ran": True, "decisions": [dataclasses.asdict(d) for d in decisions]}

"""
agents/trading_intelligence/institutional_flow_backtest.py -- a real,
historical-option-chain-archive backtest for institutional_intelligence.
institutional_flow_findings() (the "InstitutionalBuying"/"InstitutionalSelling"
heuristic), replaying backtest.load_cycles()'s own real cycles/strikes rows
through the SAME detector logic the live system runs every cycle -- the same
"replay real archived data through live logic" technique backtest.py already
uses for oi_engine's core signals, and structure_backtest.py (Milestone 20
Phase 7) already used for institutional_levels.detect_role_reversal().

Deliberately does NOT backtest institutional_intelligence.gamma_trap_findings()
-- it requires a real historical `expiry_date`, and this repo has no
historically-reconstructable expiry_date anywhere: `cycles`/`strikes`/
market_structure_snapshots have no expiry column, and the only expiry-
resolution path (expiry_intelligence.get_nearest_expiry()) requires a live,
currently-logged-in broker session reading TODAY's instrument master, which by
NSE/MCX design never retains already-expired series -- it structurally cannot
answer "what was the nearest expiry as of 2026-07-13." Only
institutional_flow_findings() is backtested here.

Known pre-existing discrepancy this backtest measures AS-IS, deliberately not
fixed here: institutional_flow_findings()'s two compute_volume_expansion()
calls omit expansion_mult=, so they silently run against that function's own
default (1.5) instead of this module's documented INSTITUTIONAL_OI_EXPANSION_
MULT (2.0). This backtest validates the detector exactly as it currently runs
live -- that's the honest thing to measure, since it's what production is
actually doing. The mismatch is a separate, distinct finding for a future fix
decision, not addressed by this module.

Never touches a broker, never opens a trade, never writes anywhere -- pure
read (via backtest.load_cycles()'s existing safe concurrent-read connection,
the same one backtest.py itself already uses live in production today) +
arithmetic. See test_institutional_flow_backtest.py's TestNoWritesToDatabase
for an automated proof, not just this docstring's claim.
"""
import collections
import dataclasses
import datetime as dt
import statistics
from unittest import mock

from oi_engine import net_oi_buildup_lean

from . import data_access, institutional_intelligence
from .. import config

# Mirrors backtest.py's own MAX_HOLD_MINUTES (currently 30) -- kept as a
# local constant rather than importing `backtest` at module scope, matching
# data_access.py's own established convention of only ever importing
# `backtest` lazily inside the function that needs it (load_candles()),
# since backtest.py pulls in a much heavier dependency chain (pandas,
# expiry_intelligence, market_structure, sr_engine_v3, exit_engine_v4...)
# than this module otherwise needs just to be imported.
OUTCOME_LOOKFORWARD_MINUTES = 30
MIN_VOLATILITY_HISTORY_CYCLES = 20   # same "don't trust a small sample" floor as BACKTEST_MIN_SAMPLE_SIZE
BACKTEST_MIN_SAMPLE_SIZE = 20   # never report a win rate below this many resolved outcomes
RECENT_STRIKE_HISTORY_DEPTH = 10   # must match what institutional_flow_findings() itself requests


@dataclasses.dataclass
class InstitutionalFlowBacktestResult:
    symbol: str
    date_from: str
    date_to: str
    sample_size: int
    wins: int
    losses: int
    pending: int
    excluded_insufficient_history: int   # honest "couldn't judge yet" bucket -- never silently dropped
    win_rate: float | None   # None (not a fabricated number) below BACKTEST_MIN_SAMPLE_SIZE


def _predicted_direction(side: str, signal: str) -> str:
    """Reuses oi_engine.net_oi_buildup_lean() -- this codebase's own
    already-established CE/PE buildup -> BULLISH/BEARISH mapping -- rather
    than a new hand-rolled lookup. A single institutional_flow_findings()
    finding is single-sided by construction, so the OTHER side is forced
    Neutral; `overall` is always BULLISH or BEARISH in practice here since
    institutional_flow_findings() only ever fires for signal in
    ("Long Buildup", "Short Buildup")."""
    if side == "CE":
        lean = net_oi_buildup_lean(signal, "Neutral")
    else:
        lean = net_oi_buildup_lean("Neutral", signal)
    return lean["overall"]


def _cycle_dt(cycle: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(cycle["ts"])


def _win_loss_threshold(cycles: list, idx: int) -> float | None:
    """`institutional_intelligence.INSTITUTIONAL_OI_EXPANSION_MULT * sigma`,
    where sigma is the population stdev of cycle-to-cycle underlying_ltp
    differences over the trailing MIN_VOLATILITY_HISTORY_CYCLES same-day
    cycles strictly before `cycles[idx]` -- a real, self-contained,
    price-only technique (mirroring structure_backtest.py's own precedent
    of preferring "a real, well-understood, price-only technique" over
    reaching for a live-only artifact that can't be honestly reconstructed
    -- market_structure_snapshots.atr_14 is only ~28% populated and
    one-snapshot-per-day, not a usable rolling intraday series), scaled by
    the SAME "genuinely elevated vs. this instrument's own recent history"
    multiplier institutional_flow_findings() already applies to the OI/
    volume side of this exact detector -- reused here for the outcome side
    rather than an unrelated new constant. Returns None (never a fabricated
    number) if fewer than MIN_VOLATILITY_HISTORY_CYCLES+1 same-day prior
    readings exist yet (e.g. the first few minutes after market open)."""
    entry_date = cycles[idx]["cycle"]["date"]
    ltps = []
    j = idx - 1
    while j >= 0 and cycles[j]["cycle"]["date"] == entry_date and len(ltps) <= MIN_VOLATILITY_HISTORY_CYCLES:
        ltps.append(cycles[j]["cycle"]["underlying_ltp"])
        j -= 1
    if len(ltps) < MIN_VOLATILITY_HISTORY_CYCLES + 1:
        return None
    ltps.reverse()   # oldest-first, so diffs are in chronological order (doesn't affect stdev, but keeps intent clear)
    diffs = [b - a for a, b in zip(ltps, ltps[1:])]
    sigma = statistics.pstdev(diffs)
    return institutional_intelligence.INSTITUTIONAL_OI_EXPANSION_MULT * sigma


def _walk_forward_outcome(cycles: list, *, start_idx: int, direction: str, threshold: float) -> str:
    """"WIN" (underlying moves >= threshold in the predicted direction),
    "LOSS" (moves >= threshold against it first -- checked before WIN each
    cycle, so if a design ever changes to allow both thresholds crossing on
    the same reading, the loss wins, never the more favorable reading, same
    ordering rule structure_backtest._walk_forward_outcome already
    established for its own OHLC-candle case; with this module's own
    single-scalar underlying_ltp reading per cycle rather than a candle's
    high/low, a genuine same-cycle tie can't actually occur for a positive
    threshold), or "PENDING" (neither resolves within
    OUTCOME_LOOKFORWARD_MINUTES of the entry cycle, OR the trading day ends
    first -- this walk-forward deliberately never crosses into the next
    day's cycles, since an overnight gap is a different phenomenon than
    intraday continuation)."""
    entry = cycles[start_idx]["cycle"]
    entry_ts = _cycle_dt(entry)
    entry_date = entry["date"]
    entry_price = entry["underlying_ltp"]

    for c in cycles[start_idx + 1:]:
        cyc = c["cycle"]
        if cyc["date"] != entry_date:
            break
        if (_cycle_dt(cyc) - entry_ts) > dt.timedelta(minutes=OUTCOME_LOOKFORWARD_MINUTES):
            break
        move = cyc["underlying_ltp"] - entry_price
        if direction == "BULLISH":
            if move <= -threshold:
                return "LOSS"
            if move >= threshold:
                return "WIN"
        else:
            if move >= threshold:
                return "LOSS"
            if move <= -threshold:
                return "WIN"
    return "PENDING"


def _make_bounded_recent_strike_history(history_by_strike: dict):
    """Returns a drop-in replacement for data_access.recent_strike_history()
    that serves ONLY strictly-historical data already fed into
    `history_by_strike` (a dict[strike, deque(maxlen=RECENT_STRIKE_HISTORY_
    DEPTH)] built incrementally, one cycle at a time, by backtest_symbol()
    below) -- fixing a real lookahead-bias bug: institutional_flow_findings()
    internally calls data_access.recent_strike_history(symbol, strike,
    limit=10), which has NO date bound and always queries the LIVE most-
    recent 10 cycles from the real cycles/strikes tables. Replaying an old
    cycle through institutional_flow_findings() unmodified would silently
    leak today's data into every historical detection.

    Newest-first, matching recent_strike_history()'s own `ORDER BY ts DESC`
    (institutional_flow_findings() reads history[1:] -- history[0] is
    "this cycle," matching production's own semantics exactly, since this
    cycle's own row is fed into the deque BEFORE institutional_flow_
    findings() is called)."""
    def _bounded_lookup(symbol, strike, *, limit=RECENT_STRIKE_HISTORY_DEPTH):
        history = history_by_strike.get(strike)
        if not history:
            return []
        return list(history)[::-1][:limit]
    return _bounded_lookup


def backtest_symbol(symbol: str, date_from: str, date_to: str, *, cycles: list | None = None,
                     ) -> InstitutionalFlowBacktestResult:
    """Runs institutional_flow_findings() against every real historical
    cycle for `symbol` between `date_from`/`date_to` (inclusive), walking
    chronologically in a single pass -- fixing the lookahead-bias bug above
    via one process-scoped monkeypatch of data_access.recent_strike_history
    for the duration of this call only (restored on exit, even on
    exception; never called from app.py, never wired into a live route --
    the patch is process-global for its duration, a real hazard if this
    were ever run concurrently with a live TI cycle in another thread).

    A (strike, side) dedup cooldown avoids treating institutional_flow_
    findings()'s own re-firing on the SAME (strike, side) across many
    consecutive ~7-15s cycles (while OI stays elevated) as independent
    samples -- that would badly inflate sample_size with correlated,
    non-independent observations of the same institutional event. Once a
    (strike, side) event opens, no new event opens for that pair until its
    own OUTCOME_LOOKFORWARD_MINUTES outcome window elapses.

    `cycles`: pass backtest.load_cycles()'s own return shape directly for a
    test (pure-synthetic, no DB I/O); left None, fetches the real archive.
    Never raises -- an empty/short `cycles` list simply reports zero
    samples."""
    if cycles is None:
        import backtest   # lazy -- see OUTCOME_LOOKFORWARD_MINUTES's own comment above
        cycles = backtest.load_cycles(symbol, date_from, date_to)

    wins = losses = pending = excluded = 0
    history_by_strike: dict[int, collections.deque] = collections.defaultdict(
        lambda: collections.deque(maxlen=RECENT_STRIKE_HISTORY_DEPTH)
    )
    cooldown_until: dict[tuple, dt.datetime] = {}

    with mock.patch.object(data_access, "recent_strike_history",
                            side_effect=_make_bounded_recent_strike_history(history_by_strike)):
        for idx, entry in enumerate(cycles):
            cyc, rows = entry["cycle"], entry["rows"]
            cyc_ts = _cycle_dt(cyc)

            # Feed this cycle's own rows into history BEFORE calling the
            # detector -- reproduces production's own history[0] == this
            # cycle, history[1:] == the prior RECENT_STRIKE_HISTORY_DEPTH-1
            # cycles semantics exactly.
            for row in rows:
                history_by_strike[row.strike].append(dataclasses.asdict(row))

            findings = institutional_intelligence.institutional_flow_findings(symbol, rows)
            if not findings:
                continue

            threshold = _win_loss_threshold(cycles, idx)
            for f in findings:
                key = (f.strike, f.evidence["side"])
                if key in cooldown_until and cyc_ts < cooldown_until[key]:
                    continue   # same institutional event still being tracked -- not a new independent sample

                if threshold is None:
                    excluded += 1
                    continue

                direction = _predicted_direction(f.evidence["side"], f.evidence["signal"])
                outcome = _walk_forward_outcome(cycles, start_idx=idx, direction=direction, threshold=threshold)
                if outcome == "WIN":
                    wins += 1
                elif outcome == "LOSS":
                    losses += 1
                else:
                    pending += 1
                cooldown_until[key] = cyc_ts + dt.timedelta(minutes=OUTCOME_LOOKFORWARD_MINUTES)

    sample_size = wins + losses   # PENDING/excluded don't count toward a win rate -- unresolved, not a loss
    win_rate = round(wins / sample_size, 4) if sample_size >= BACKTEST_MIN_SAMPLE_SIZE else None
    return InstitutionalFlowBacktestResult(
        symbol=symbol, date_from=date_from, date_to=date_to,
        sample_size=sample_size, wins=wins, losses=losses, pending=pending,
        excluded_insufficient_history=excluded, win_rate=win_rate,
    )


def backtest_all_watched_symbols(date_from: str, date_to: str) -> dict[str, InstitutionalFlowBacktestResult]:
    """Runs backtest_symbol() for every config.TI_WATCHED_SYMBOLS entry --
    never lets one symbol's absence of logged data (e.g. a symbol added to
    the watch list after logging started) fail the whole run."""
    results = {}
    for symbol in config.TI_WATCHED_SYMBOLS:
        results[symbol] = backtest_symbol(symbol, date_from, date_to)
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Backtest institutional_flow_findings() against real historical cycles/strikes data."
    )
    parser.add_argument("--symbol", help="Single symbol to backtest (default: all TI_WATCHED_SYMBOLS)")
    parser.add_argument("--all", action="store_true", help="Backtest every config.TI_WATCHED_SYMBOLS entry")
    parser.add_argument("--from", dest="date_from", required=True, help="Start date, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", required=True, help="End date, YYYY-MM-DD")
    args = parser.parse_args()

    if args.all or not args.symbol:
        all_results = backtest_all_watched_symbols(args.date_from, args.date_to)
    else:
        all_results = {args.symbol: backtest_symbol(args.symbol, args.date_from, args.date_to)}

    header = f"{'symbol':<14}{'sample':>8}{'wins':>7}{'losses':>8}{'pending':>9}{'excluded':>10}{'win_rate':>10}"
    print(header)
    print("-" * len(header))
    for symbol, r in all_results.items():
        win_rate_str = f"{r.win_rate:.2%}" if r.win_rate is not None else "N/A"
        print(f"{symbol:<14}{r.sample_size:>8}{r.wins:>7}{r.losses:>8}{r.pending:>9}"
              f"{r.excluded_insufficient_history:>10}{win_rate_str:>10}")

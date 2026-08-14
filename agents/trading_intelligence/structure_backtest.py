"""
agents/trading_intelligence/structure_backtest.py -- Milestone 20, Phase 7:
a real, historical-candle-archive backtest for institutional_levels.
detect_role_reversal()'s own tunable parameters (MAX_RETEST_CANDLES,
MIN_VOLUME_MULTIPLIER). Pure read + arithmetic over data_access.
load_candles()'s existing daily archive -- never touches a broker, never
opens a trade, never writes anywhere on its own (structure_tuning.py,
Phase 7's other half, is the only writer, and only to its own audit log).

Deliberately does NOT replay weighted_levels() (which needs a live OI
snapshot at every historical instant -- not something the candle archive
alone can reconstruct). Instead it identifies candidate levels the same
way any standard swing-high/low fractal does: a real, well-understood,
price-only technique, independent of (and complementary to) the live
OI-based level scoring. This measures "does detect_role_reversal's own
breakout/retest/confirmation logic hold up historically," not "would
weighted_levels have picked this exact level" -- the right question for
tuning the reversal detector's OWN thresholds.
"""
import dataclasses

import institutional_levels as il

from . import data_access

PIVOT_WINDOW = 5   # candles on each side that must be lower/higher for a fractal pivot
OUTCOME_LOOKFORWARD_CANDLES = 100   # how far past a confirmed reversal to look for T1/SL
BACKTEST_MIN_SAMPLE_SIZE = 20   # never report a win rate below this many resolved outcomes


@dataclasses.dataclass
class BacktestResult:
    symbol: str
    max_retest_candles: int
    min_volume_multiplier: float
    sample_size: int
    wins: int
    losses: int
    pending: int
    win_rate: float | None   # None (not a fabricated number) below BACKTEST_MIN_SAMPLE_SIZE


def _find_pivots(candles: list, *, window: int = PIVOT_WINDOW) -> list:
    """Real, standard fractal pivot detection -- candle i is a swing
    high if its high is the strict max of the `window` candles on each
    side (swing low: strict min of the lows). Returns a list of
    {"index", "level", "type"} ("RESISTANCE" for a swing high, "SUPPORT"
    for a swing low) -- the candidate levels this backtest tests
    detect_role_reversal() against."""
    pivots = []
    for i in range(window, len(candles) - window):
        window_slice = candles[i - window:i + window + 1]
        high = candles[i]["high"]
        low = candles[i]["low"]
        if high == max(c["high"] for c in window_slice):
            pivots.append({"index": i, "level": high, "type": "RESISTANCE"})
        if low == min(c["low"] for c in window_slice):
            pivots.append({"index": i, "level": low, "type": "SUPPORT"})
    return pivots


def _walk_forward_outcome(candles: list, *, start_idx: int, overlay: dict) -> str:
    """"WIN" (T1 hit before SL), "LOSS" (SL hit first or hit
    simultaneously -- ties go to the stop, never the more favorable
    reading), or "PENDING" (neither resolved within
    OUTCOME_LOOKFORWARD_CANDLES of `start_idx`) -- same walk-forward
    technique used earlier (manually) to verify real channel alerts
    against actual subsequent price action, formalized here."""
    direction = overlay["direction"]
    end_idx = min(start_idx + OUTCOME_LOOKFORWARD_CANDLES, len(candles))
    for c in candles[start_idx:end_idx]:
        if direction == "BULLISH":
            if c["low"] <= overlay["sl"]:
                return "LOSS"
            if c["high"] >= overlay["t1"]:
                return "WIN"
        else:
            if c["high"] >= overlay["sl"]:
                return "LOSS"
            if c["low"] <= overlay["t1"]:
                return "WIN"
    return "PENDING"


def backtest_parameters(symbol: str, candles: list, *, max_retest_candles: int,
                         min_volume_multiplier: float) -> BacktestResult:
    """Runs detect_role_reversal() (under the given parameter override)
    against every real fractal pivot in `candles`, walks forward from
    each confirmed reversal's own retest candle to score WIN/LOSS/
    PENDING, and reports the aggregate. Never raises -- an empty
    `candles` list simply reports zero samples."""
    profile = il.get_profile(symbol)
    pivots = _find_pivots(candles)

    wins = losses = pending = 0
    for pivot in pivots:
        # A window starting at the pivot itself, long enough for a real
        # breakout/retest/confirmation to complete within
        # max_retest_candles/lookahead, PLUS room for the outcome walk-
        # forward below -- never the full remaining archive (which would
        # make this backtest re-scan overlapping tails O(n^2) times).
        window_end = min(pivot["index"] + max_retest_candles + 20 + OUTCOME_LOOKFORWARD_CANDLES, len(candles))
        window = candles[pivot["index"]:window_end]
        if len(window) < max_retest_candles + 2:
            continue

        reversal = il.detect_role_reversal(
            pivot["level"], window, profile=profile,
            max_retest_candles=max_retest_candles, min_volume_multiplier=min_volume_multiplier,
        )
        if reversal is None:
            continue
        overlay = il.compute_trade_plan_overlay(symbol, reversal)
        if overlay is None:
            continue

        # Walk forward from the retest candle's own position in `window`
        # (not `candles`) -- everything below operates in the same
        # window-local index space detect_role_reversal() was given.
        retest_dt = reversal["retest_candle"].get("datetime")
        retest_idx = next((i for i, c in enumerate(window) if c.get("datetime") == retest_dt), None)
        if retest_idx is None:
            continue
        outcome = _walk_forward_outcome(window, start_idx=retest_idx + 1, overlay=overlay)
        if outcome == "WIN":
            wins += 1
        elif outcome == "LOSS":
            losses += 1
        else:
            pending += 1

    sample_size = wins + losses   # PENDING outcomes don't count toward a win rate -- unresolved, not a loss
    win_rate = round(wins / sample_size, 4) if sample_size >= BACKTEST_MIN_SAMPLE_SIZE else None
    return BacktestResult(
        symbol=symbol, max_retest_candles=max_retest_candles, min_volume_multiplier=min_volume_multiplier,
        sample_size=sample_size, wins=wins, losses=losses, pending=pending, win_rate=win_rate,
    )


def backtest_symbol(symbol: str, *, param_grid: list | None = None, candles=None) -> list:
    """Runs backtest_parameters() for every (max_retest_candles,
    min_volume_multiplier) combination in `param_grid` (default: a
    small grid around the live institutional_levels module constants),
    returning one BacktestResult per combination, worst-to-best by win
    rate (None/insufficient-sample results last, in input order).
    `candles`: pass real candles for a test; left None, fetches the
    real archive via data_access.load_candles()."""
    if candles is None:
        df = data_access.load_candles(symbol)
        candles = df.to_dict("records") if not df.empty else []
    if param_grid is None:
        param_grid = [
            (mrc, mvm)
            for mrc in (2, il.MAX_RETEST_CANDLES, 4, 5)
            for mvm in (1.0, 1.2, il.MIN_VOLUME_MULTIPLIER, 1.5)
        ]
        param_grid = sorted(set(param_grid))

    results = [
        backtest_parameters(symbol, candles, max_retest_candles=mrc, min_volume_multiplier=mvm)
        for mrc, mvm in param_grid
    ]
    results.sort(key=lambda r: (r.win_rate is None, -(r.win_rate or 0)))
    return results

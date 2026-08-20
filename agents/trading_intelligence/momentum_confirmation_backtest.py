"""
agents/trading_intelligence/momentum_confirmation_backtest.py -- real
evidence-gathering backtest for oi_engine.generate_signal()'s
TI_ENABLE_MOMENTUM_CONFIRMATION flag (added PR #29, deployed OFF by
default, never validated against real historical data before this
module). Reuses backtest.py's existing V1 signal-replay engine
(simulate_trades(), extended additively with candles_df/
momentum_confirmation_enabled kwargs -- see that function's own updated
docstring) rather than reinventing a second backtest loop.

Runs the SAME symbol/date-range replay twice -- momentum confirmation OFF,
then ON -- and reports the real difference in trade count, win rate, and
net points. This is the evidence the flag was deployed without. See
MOMENTUM_CONFIRMATION_BACKTEST_REPORT.md (written once a real run against
the live archive has produced real numbers) for the actual finding and
the resulting decision on whether to turn the flag on in production.

Read-only: this module contains zero SQL write statements and never
touches the broker or any paper-trade table -- it only replays already-
logged historical cycles/candles through generate_signal(), exactly the
same safe pattern institutional_flow_backtest.py and
dual_probability_backtest.py already established.
"""
import dataclasses

import backtest

BACKTEST_MIN_SAMPLE_SIZE = 20   # same floor structure_backtest.py/institutional_flow_backtest.py use -- never report a win rate below this


@dataclasses.dataclass
class MomentumComparisonResult:
    symbol: str
    date_from: str
    date_to: str
    off_trade_count: int
    off_wins: int
    off_losses: int
    off_win_rate: float | None
    off_net_points: float
    on_trade_count: int
    on_wins: int
    on_losses: int
    on_win_rate: float | None
    on_net_points: float


def _summarize(trades):
    wins = sum(1 for t in trades if t["exit_reason"] == "TARGET HIT")
    losses = sum(1 for t in trades if t["exit_reason"] == "STOP LOSS")
    resolved = wins + losses   # TIME EXIT trades don't count toward a win rate -- unresolved, not a loss
    win_rate = round(wins / resolved, 4) if resolved >= BACKTEST_MIN_SAMPLE_SIZE else None
    net_points = round(sum(t["points"] for t in trades), 2) if trades else 0.0
    return len(trades), wins, losses, win_rate, net_points


def compare_symbol(symbol, date_from, date_to, *, persistence_cycles=2, cooldown_minutes=15,
                    confidence_threshold=60, candles_df=None) -> MomentumComparisonResult:
    """Replays `symbol` over [date_from, date_to] through backtest.py's
    real V1 signal engine twice -- identical inputs except for the
    momentum-confirmation flag -- so any difference in outcome is
    attributable ONLY to that flag, never a confound from different
    market data between the two runs (both share the exact same
    candles_df, loaded once).

    candles_df: override for tests (a pre-built DataFrame) -- production
    callers leave this None and get the real archive via
    backtest.load_intraday_candles()."""
    if candles_df is None:
        candles_df = backtest.load_intraday_candles(symbol, timeframe="3m")

    off_trades, _ = backtest.simulate_trades(
        symbol, date_from, date_to, persistence_cycles, cooldown_minutes, confidence_threshold,
        momentum_confirmation_enabled=False, candles_df=candles_df,
    )
    on_trades, _ = backtest.simulate_trades(
        symbol, date_from, date_to, persistence_cycles, cooldown_minutes, confidence_threshold,
        momentum_confirmation_enabled=True, candles_df=candles_df,
    )

    off_count, off_wins, off_losses, off_rate, off_net = _summarize(off_trades)
    on_count, on_wins, on_losses, on_rate, on_net = _summarize(on_trades)

    return MomentumComparisonResult(
        symbol=symbol, date_from=date_from, date_to=date_to,
        off_trade_count=off_count, off_wins=off_wins, off_losses=off_losses,
        off_win_rate=off_rate, off_net_points=off_net,
        on_trade_count=on_count, on_wins=on_wins, on_losses=on_losses,
        on_win_rate=on_rate, on_net_points=on_net,
    )


def compare_all_watched_symbols(date_from, date_to) -> dict:
    from agents import config
    return {symbol: compare_symbol(symbol, date_from, date_to) for symbol in config.TI_WATCHED_SYMBOLS}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compare oi_engine signal outcomes with momentum confirmation OFF vs ON.")
    parser.add_argument("--symbol", help="Single symbol to backtest (default: all TI_WATCHED_SYMBOLS)")
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if args.all or not args.symbol:
        results = compare_all_watched_symbols(args.date_from, args.date_to)
        for symbol, r in results.items():
            print(r)
    else:
        print(compare_symbol(args.symbol, args.date_from, args.date_to))

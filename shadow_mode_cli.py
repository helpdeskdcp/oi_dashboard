"""
shadow_mode_cli.py -- Milestone 12, Phase 2B: Market-Open Observation
Validation. Manual operator CLI for agents.shadow_mode -- the ONLY way
observer.observe_and_predict()/evaluator.evaluate_pending() are ever
invoked in this codebase. Neither function has any automatic caller
anywhere (no scheduler wiring, no background thread, no HTTP write
route) -- this script exists specifically so a human can run one
observe-then-predict cycle, or sweep pending evaluations, on demand
during a live market session, without adding any autonomous execution
path. Mirrors runtime_control_cli.py's own established pattern: a thin
wrapper, every action here calls straight into agents.shadow_mode.X,
the exact same functions a future dashboard/API trigger would call.

Usage:
    python3 shadow_mode_cli.py observe NIFTY [--timeframe 3m]
        # one observe-then-predict cycle for one symbol. Reads
        # already-stored market data only (no broker call); writes
        # exactly one shadow_observations row and, if a usable signal
        # was computed, exactly one shadow_predictions row. No-op
        # (prints "no snapshot available") if no cycle has been logged
        # for this symbol yet.

    python3 shadow_mode_cli.py evaluate [--limit 100]
        # evaluates every pending prediction (no shadow_outcomes row
        # yet) against already-archived candles. Writes zero or more
        # shadow_outcomes rows; never touches shadow_observations or
        # shadow_predictions.

    python3 shadow_mode_cli.py status
        # read-only: the same payload GET /api/shadow/status returns.

Every write this script can ever perform lands in shadow_observations,
shadow_predictions, or shadow_outcomes -- and nowhere else. No broker
module, no paper_orders/paper_trades table, no scheduler, no runtime-
control flag is ever touched.
"""
import argparse
import datetime as dt

from agents.shadow_mode import api as shadow_api
from agents.shadow_mode import evaluator, observer


def _cmd_observe(args):
    result = observer.observe_and_predict(args.symbol, timeframe=args.timeframe)
    if result is None:
        print(f"{args.symbol}: no market snapshot available yet -- nothing recorded.")
        return
    signal = result["signal"]
    print(f"{args.symbol}: observation_id={result['observation_id']} prediction_id={result['prediction_id']}")
    print(f"  signal_type={signal.get('action')!r} direction={signal.get('direction')!r} "
          f"confidence={signal.get('confidence')!r}")
    print(f"  reason: {signal.get('reason')}")


def _cmd_evaluate(args):
    outcomes = evaluator.evaluate_pending(limit=args.limit)
    print(f"Evaluated {len(outcomes)} prediction(s).")
    for outcome in outcomes:
        print(f"  prediction_id={outcome['prediction_id']} -> {outcome['classification']}")


def _cmd_status(args):
    status = shadow_api.get_status()
    for key, value in status.items():
        print(f"{key}: {value}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_observe = sub.add_parser("observe", help="run one observe-then-predict cycle for a symbol")
    p_observe.add_argument("symbol")
    p_observe.add_argument("--timeframe", default=observer.DEFAULT_TIMEFRAME)
    p_observe.set_defaults(func=_cmd_observe)

    p_evaluate = sub.add_parser("evaluate", help="evaluate every pending prediction")
    p_evaluate.add_argument("--limit", type=int, default=100)
    p_evaluate.set_defaults(func=_cmd_evaluate)

    p_status = sub.add_parser("status", help="read-only Shadow Mode status")
    p_status.set_defaults(func=_cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

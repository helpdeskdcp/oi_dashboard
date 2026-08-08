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

    python3 shadow_mode_cli.py observe NIFTY --dry-run
        # runs the SAME market-snapshot read and signal-generation
        # primitives, prints the observation/prediction payload that
        # WOULD be written, and performs ZERO database writes. Safe to
        # run at any time, including outside market hours or against
        # production, with no risk of polluting shadow_observations/
        # shadow_predictions with non-representative rows.

    python3 shadow_mode_cli.py observe NIFTY --dry-run --export-json out.json
        # same as above, plus writes the computed payload as structured
        # JSON to the given path (only that path -- never touches the
        # database). Requires --dry-run (there is no "write to DB AND
        # export" mode).

    python3 shadow_mode_cli.py evaluate [--limit 100]
        # evaluates every pending prediction (no shadow_outcomes row
        # yet) against already-archived candles. Writes zero or more
        # shadow_outcomes rows; never touches shadow_observations or
        # shadow_predictions.

    python3 shadow_mode_cli.py evaluate --dry-run [--limit 100]
        # same classification logic, printed only -- ZERO database
        # writes, including to already-evaluated predictions (dry-run
        # recomputes and shows what the classification WOULD be).

    python3 shadow_mode_cli.py status
        # read-only: the same payload GET /api/shadow/status returns.

Every write this script can ever perform (outside --dry-run) lands in
shadow_observations, shadow_predictions, or shadow_outcomes -- and
nowhere else. No broker module, no paper_orders/paper_trades table, no
scheduler, no runtime-control flag is ever touched. --dry-run modes
perform no database write of any kind and no network side effect beyond
the already-existing market-data reads observe/evaluate always perform.
"""
import argparse
import datetime as dt
import json
import sys

from agents.shadow_mode import api as shadow_api
from agents.shadow_mode import evaluator, observer, store

DRY_RUN_BANNER = "DRY RUN — NO DATABASE WRITES PERFORMED"


def _build_export_payload(symbol: str, computed: dict, *, now: dt.datetime) -> dict:
    """The structured JSON snapshot --export-json writes. Every value
    here comes straight from observer.compute_observation_and_predict()'s
    own return shape -- no re-derivation, no second read."""
    observation, prediction, signal = computed["observation"], computed["prediction"], computed["signal"]
    return {
        "timestamp": now.isoformat(),
        "symbol": symbol,
        "timeframe": observation["timeframe"],
        "signal_inputs": {
            "underlying_ltp": observation["underlying_ltp"], "atm": observation["atm"], "pcr": observation["pcr"],
            "market_bias": observation["market_bias"], "bias_note": observation["bias_note"],
        },
        "generated_signal": signal,
        "confidence": prediction["confidence"],
        "expected_direction": prediction["expected_direction"],
        "entry_reference_price": prediction["entry_reference_price"],
        "target_price": signal.get("target_price"),
        "sl_price": signal.get("sl_price"),
        "expected_target_low": prediction["expected_target_low"],
        "expected_target_high": prediction["expected_target_high"],
        "metadata": {"dry_run": True, "cli": "shadow_mode_cli.py", "generated_at": now.isoformat()},
    }


def _write_export_json(path: str, payload: dict) -> None:
    try:
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)
    except OSError as exc:
        print(f"Error: could not write export file {path!r}: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Exported JSON snapshot to {path}")


def _cmd_observe(args):
    if args.export_json and not args.dry_run:
        print("Error: --export-json requires --dry-run (no combined write+export mode exists).", file=sys.stderr)
        sys.exit(2)

    if args.dry_run:
        now = dt.datetime.now()
        computed = observer.compute_observation_and_prediction(args.symbol, timeframe=args.timeframe, now=now)
        if computed is None:
            print(f"{args.symbol}: no market snapshot available yet -- nothing to show.")
            print()
            print(DRY_RUN_BANNER)
            return
        signal = computed["signal"]
        print(f"{args.symbol}: DRY RUN observation + prediction payload")
        print(f"  observation: {computed['observation']}")
        print(f"  prediction:  {computed['prediction']}")
        print(f"  signal_type={signal.get('action')!r} direction={signal.get('direction')!r} "
              f"confidence={signal.get('confidence')!r}")
        print(f"  reason: {signal.get('reason')}")
        print()
        print(DRY_RUN_BANNER)
        if args.export_json:
            _write_export_json(args.export_json, _build_export_payload(args.symbol, computed, now=now))
        return

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
    if args.dry_run:
        pending = store.list_predictions_pending_evaluation(limit=args.limit)
        print(f"DRY RUN: {len(pending)} pending prediction(s) to classify")
        for prediction in pending:
            result = evaluator.dry_run_evaluate_prediction(prediction["id"])
            if result is None:
                continue
            if result["pending"]:
                print(f"  prediction_id={result['prediction_id']} -> still pending ({result['reason']})")
            else:
                print(f"  prediction_id={result['prediction_id']} -> would be classified: {result['classification']}")
        print()
        print(DRY_RUN_BANNER)
        return

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
    p_observe.add_argument("--dry-run", action="store_true", help="compute and display only -- zero database writes")
    p_observe.add_argument("--export-json", metavar="PATH", help="write the computed payload as JSON (requires --dry-run)")
    p_observe.set_defaults(func=_cmd_observe)

    p_evaluate = sub.add_parser("evaluate", help="evaluate every pending prediction")
    p_evaluate.add_argument("--limit", type=int, default=100)
    p_evaluate.add_argument("--dry-run", action="store_true", help="classify and display only -- zero database writes")
    p_evaluate.set_defaults(func=_cmd_evaluate)

    p_status = sub.add_parser("status", help="read-only Shadow Mode status")
    p_status.set_defaults(func=_cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

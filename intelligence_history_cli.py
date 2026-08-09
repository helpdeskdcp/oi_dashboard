"""
intelligence_history_cli.py -- Milestone 13, Phase 2: Live Observational
Validation. Manual operator CLI for agents.intelligence_history -- the
ONLY way a MarketIntelligenceSnapshot ever gets logged to
intelligence_snapshots_log in this codebase. Nothing calls
store.record_snapshot() anywhere else (no scheduler wiring, no
background thread, no HTTP write route) -- this script exists
specifically so a human can log one snapshot, or a batch over time by
re-running it, on demand during a live market session, without adding
any autonomous execution path. Mirrors shadow_mode_cli.py's own
established pattern exactly.

Usage:
    python3 intelligence_history_cli.py log NIFTY [--timeframe 3m]
        # computes intelligence_orchestrator.build_snapshot(symbol) and
        # writes exactly one intelligence_snapshots_log row. No-op
        # (prints "no snapshot available") if build_snapshot() returns
        # None (no market data logged yet for this symbol).

    python3 intelligence_history_cli.py log NIFTY --dry-run
        # computes the SAME snapshot, prints it, performs ZERO database
        # writes. Safe to run at any time, including outside market
        # hours or against production.

    python3 intelligence_history_cli.py log NIFTY --dry-run --export-json out.json
        # same as above, plus writes the computed snapshot as structured
        # JSON to the given path (only that path -- never touches the
        # database). Requires --dry-run.

    python3 intelligence_history_cli.py report --symbol NIFTY [--since-ts ISO]
        # read-only: prints agents.intelligence_history.api.get_report()
        # -- bias stability, confidence stability, greeks coherence, OI
        # responsiveness, bias/price correlation, computed over
        # already-logged history. Zero writes.

    python3 intelligence_history_cli.py status
        # read-only: the same payload GET /api/intelligence/history/status
        # returns.

Every write this script can ever perform (outside --dry-run) lands in
intelligence_snapshots_log -- and nowhere else. No broker module, no
paper_orders/paper_trades table, no scheduler, no runtime-control flag is
ever touched. --dry-run performs no database write of any kind.
"""
import argparse
import datetime as dt
import json
import sys

import intelligence_orchestrator
from agents.intelligence_history import api as history_api
from agents.intelligence_history import store

DRY_RUN_BANNER = "DRY RUN — NO DATABASE WRITES PERFORMED"


def _write_export_json(path: str, payload: dict) -> None:
    try:
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)
    except OSError as exc:
        print(f"Error: could not write export file {path!r}: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Exported JSON snapshot to {path}")


def _cmd_log(args):
    if args.export_json and not args.dry_run:
        print("Error: --export-json requires --dry-run (no combined write+export mode exists).", file=sys.stderr)
        sys.exit(2)

    snapshot = intelligence_orchestrator.build_snapshot(args.symbol, timeframe=args.timeframe)
    if snapshot is None:
        print(f"{args.symbol}: no market snapshot available yet -- nothing to log.")
        if args.dry_run:
            print()
            print(DRY_RUN_BANNER)
        return

    now = dt.datetime.now()
    payload = snapshot.to_dict()

    if args.dry_run:
        print(f"{args.symbol}: DRY RUN snapshot payload")
        for key, value in payload.items():
            print(f"  {key}: {value}")
        print()
        print(DRY_RUN_BANNER)
        if args.export_json:
            export_payload = {
                "timestamp": now.isoformat(), "timeframe": args.timeframe, "snapshot": payload,
                "metadata": {"dry_run": True, "cli": "intelligence_history_cli.py", "generated_at": now.isoformat()},
            }
            _write_export_json(args.export_json, export_payload)
        return

    row_id = store.record_snapshot(ts=now.isoformat(), symbol=args.symbol, timeframe=args.timeframe, snapshot=snapshot)
    print(f"{args.symbol}: logged snapshot id={row_id}")
    for key, value in payload.items():
        print(f"  {key}: {value}")


def _cmd_report(args):
    report = history_api.get_report(symbol=args.symbol, since_ts=args.since_ts)
    print(f"Intelligence history report for {args.symbol}" + (f" since {args.since_ts}" if args.since_ts else ""))
    for section, values in report.items():
        if section in ("symbol", "since_ts"):
            continue
        print(f"  {section}:")
        for key, value in values.items():
            print(f"    {key}: {value}")


def _cmd_status(args):
    status = history_api.get_status()
    for key, value in status.items():
        print(f"{key}: {value}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_log = sub.add_parser("log", help="compute and log one intelligence snapshot for a symbol")
    p_log.add_argument("symbol")
    p_log.add_argument("--timeframe", default=intelligence_orchestrator.DEFAULT_TIMEFRAME)
    p_log.add_argument("--dry-run", action="store_true", help="compute and display only -- zero database writes")
    p_log.add_argument("--export-json", metavar="PATH", help="write the computed snapshot as JSON (requires --dry-run)")
    p_log.set_defaults(func=_cmd_log)

    p_report = sub.add_parser("report", help="drift/stability report over already-logged history")
    p_report.add_argument("--symbol", required=True)
    p_report.add_argument("--since-ts", default=None)
    p_report.set_defaults(func=_cmd_report)

    p_status = sub.add_parser("status", help="read-only intelligence history status")
    p_status.set_defaults(func=_cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

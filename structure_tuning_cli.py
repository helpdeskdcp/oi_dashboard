"""
structure_tuning_cli.py -- Milestone 20, Phase 7: manual operator CLI
for agents.trading_intelligence.structure_tuning, the adaptive
structure-detection tuning loop. The loop itself is ALSO wired into the
live TI cycle (agents/runtime/agent_runtime.py's _trading_intelligence_
cycle) -- see structure_tuning.py's own docstring for why that's still
safe (hard bounds, minimum sample size, minimum improvement margin,
cooldown, full audit log). This script is the manual/on-demand
entrypoint: force an evaluation right now (bypassing the internal
TUNING_EVALUATION_INTERVAL_HOURS gate), preview what it WOULD do
without applying anything, or just review the audit log.

Usage:
    python3 structure_tuning_cli.py run
        # forces one evaluation pass (bypasses the interval gate),
        # applies any change that clears all safety gates, logs every
        # decision either way.

    python3 structure_tuning_cli.py run --dry-run
        # same evaluation, same audit log entries, but NEVER mutates
        # institutional_levels' live MAX_RETEST_CANDLES/
        # MIN_VOLUME_MULTIPLIER constants.

    python3 structure_tuning_cli.py backtest NIFTY [--symbol ...]
        # runs structure_backtest.backtest_symbol() for one symbol
        # against its real candle archive, prints the full parameter
        # grid ranked by win rate. Read-only, no DB writes at all.

    python3 structure_tuning_cli.py history [--parameter max_retest_candles] [--limit 20]
        # prints the audit log -- the same rows GET /api/structure/
        # tuning/history returns.
"""
import argparse
import json

import institutional_levels as il
from agents import config as agents_config
from agents.trading_intelligence import structure_backtest, structure_tuning


def _cmd_run(args):
    structure_tuning.init_db()
    result = structure_tuning.evaluate_and_maybe_tune(
        agents_config.TI_WATCHED_SYMBOLS, apply=not args.dry_run, force=True,
    )
    print(json.dumps(result, indent=2, default=str))


def _cmd_backtest(args):
    results = structure_backtest.backtest_symbol(args.symbol)
    print(f"=== {args.symbol} -- backtest grid (best win rate first) ===")
    for r in results:
        rate = f"{r.win_rate:.1%}" if r.win_rate is not None else "insufficient sample"
        print(f"  max_retest_candles={r.max_retest_candles} min_volume_multiplier={r.min_volume_multiplier}: "
              f"win_rate={rate} (wins={r.wins} losses={r.losses} pending={r.pending})")


def _cmd_history(args):
    structure_tuning.init_db()
    print("Current live values:")
    for name, spec in structure_tuning.TUNABLE_PARAMS.items():
        print(f"  {name} = {getattr(il, spec['attr'])}")
    print()
    rows = structure_tuning.list_tuning_history(parameter=args.parameter, limit=args.limit)
    if not rows:
        print("No tuning evaluations logged yet.")
        return
    for row in rows:
        applied = "APPLIED" if row["applied"] else "not applied"
        print(f"  [{row['ts']}] {row['parameter']}: {row['current_value']} -> {row['best_candidate']} "
              f"({applied}) -- {row['reason']}")


def main():
    parser = argparse.ArgumentParser(description="Adaptive structure-tuning loop -- manual operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="force one evaluation pass now")
    p_run.add_argument("--dry-run", action="store_true", help="evaluate and log, but never mutate live constants")
    p_run.set_defaults(func=_cmd_run)

    p_backtest = sub.add_parser("backtest", help="run the parameter grid backtest for one symbol")
    p_backtest.add_argument("symbol")
    p_backtest.set_defaults(func=_cmd_backtest)

    p_history = sub.add_parser("history", help="print the tuning audit log")
    p_history.add_argument("--parameter", default=None)
    p_history.add_argument("--limit", type=int, default=20)
    p_history.set_defaults(func=_cmd_history)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

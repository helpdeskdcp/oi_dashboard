"""
trading_intelligence_cli.py -- manual operator CLI for the AI Trading
Engine's signal -> activation -> paper-trade lifecycle
(agents/trading_intelligence/).

Root cause of the "ti_paper_trades has 0 rows" finding (Today Signal
Audit, 2026-08-10): agents.trading_intelligence.api.run_scheduled_cycle()
-- the only function that ever calls paper_trading.enter_from_recommendation()
-> ti_store.open_trade() -- is only invoked by the Milestone 9 Runtime
Scheduler's "trading_intelligence" agent cycle, which is blocked at two
independent, deliberate layers: RUNTIME_SCHEDULER_ENABLED=false, and
"trading_intelligence" in agents.runtime.scheduling_control.
NEVER_SCHEDULABLE_AGENTS (a hard, code-level constant -- see that
module's own docstring for why). This script does NOT touch either of
those locks. It is a THIRD, separate, manual-only entrypoint that calls
run_scheduled_cycle() directly -- the exact same "human explicitly runs
a command" pattern shadow_mode_cli.py / intelligence_history_cli.py /
intelligence_alerts_cli.py already established for their own engines.
Nothing here starts a thread, a loop, or a scheduled/recurring job of
any kind -- one invocation runs exactly one cycle, then exits.

Usage:
    python3 trading_intelligence_cli.py run-cycle
        # calls agents.trading_intelligence.api.run_scheduled_cycle() --
        # for every config.TI_WATCHED_SYMBOLS symbol: builds one
        # snapshot, evaluates one AI Trading Engine recommendation, and
        # (only for an actionable BUY CE/PE recommendation) opens one
        # ti_paper_trades row. THE ONLY COMMAND HERE THAT WRITES
        # ANYTHING. Never touches a broker (run_scheduled_cycle()'s own
        # docstring: "Still never touches the broker").

    python3 trading_intelligence_cli.py today
        # read-only: every ti_paper_trades row (open + closed) whose
        # entry_time is today, with live unrealized P&L for open rows
        # (fetched from already-stored market_data.get_snapshot() data,
        # never a live broker call).

    python3 trading_intelligence_cli.py audit
        # read-only: today's signals/activations/target-hits/SL-hits/
        # win-rate/net-P&L summary, cross-checked against ti_signal_log
        # and ti_paper_trades directly.
"""
import argparse
import datetime as dt
import sys

from agents.trading_intelligence import api as ti_api
from agents.trading_intelligence import market_data, ti_store


def _today_prefix() -> str:
    return dt.date.today().isoformat()


def _unrealized_pnl(trade: dict) -> float | None:
    """Same lookup _check_open_trade_exit() uses (already-stored
    snapshot data, never a live fetch) -- None (not 0) when the current
    premium genuinely isn't available, so a missing price is never
    silently reported as flat P&L."""
    snapshot = market_data.get_snapshot(trade["symbol"])
    if not snapshot.available:
        return None
    row = next((r for r in snapshot.strikes if r.strike == trade["strike"]), None)
    if row is None:
        return None
    current_ltp = row.ce_ltp if trade["direction"] == "CE" else row.pe_ltp
    if not current_ltp or current_ltp <= 0:
        return None
    return round((current_ltp - trade["entry_price"]) * trade["qty"], 2)


def _cmd_run_cycle(args) -> int:
    try:
        results = ti_api.run_scheduled_cycle()
    except Exception as e:
        print(f"Error: run_scheduled_cycle() failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    for symbol, r in results.items():
        if not r["available"]:
            print(f"{symbol}: unavailable ({r['reason']})")
            continue
        line = f"{symbol}: action={r['action']} trade_opened={r['trade_opened']}"
        if r["trade_opened"]:
            line += f" trade_id={r['trade_id']}"
        print(line)
    return 0


def _cmd_today(args) -> int:
    today = _today_prefix()
    open_trades = [t for t in ti_store.list_open_trades() if t["entry_time"].startswith(today)]
    closed_trades = [t for t in ti_store.list_closed_trades(limit=10_000) if t["entry_time"].startswith(today)]
    trades = sorted(open_trades + closed_trades, key=lambda t: t["entry_time"])

    if not trades:
        print(f"No ti_paper_trades rows with entry_time today ({today}).")
        return 0

    print(f"{'ID':>4}  {'Open Time':<26} {'Symbol':<12} {'Dir':<4} {'Entry':>10} {'Status':<8} "
          f"{'Target':>10} {'SL':>10} {'P&L':>12}")
    for t in trades:
        if t["status"] == "OPEN":
            pnl = _unrealized_pnl(t)
            pnl_str = f"{pnl:+.2f} (unrl)" if pnl is not None else "N/A (unrl)"
        else:
            pnl_str = f"{t['points']:+.2f} (real)" if t["points"] is not None else "NO DATA FOUND"
        print(f"{t['id']:>4}  {t['entry_time']:<26} {t['symbol']:<12} {t['direction']:<4} "
              f"{t['entry_price']:>10.2f} {t['status']:<8} {t['target_price'] or 0:>10.2f} "
              f"{t['sl_price'] or 0:>10.2f} {pnl_str:>12}")
    return 0


def _cmd_audit(args) -> int:
    today = _today_prefix()
    signals_today = [s for s in ti_store.list_signals(limit=100_000) if s["ts"].startswith(today)]
    actionable = [s for s in signals_today if s["action"] in ("BUY CE", "BUY PE")]

    open_trades = [t for t in ti_store.list_open_trades() if t["entry_time"].startswith(today)]
    closed_trades = [t for t in ti_store.list_closed_trades(limit=10_000) if t["entry_time"].startswith(today)]
    opened_today = open_trades + closed_trades

    target_hits = [t for t in closed_trades if t["exit_reason"] == "TARGET HIT"]
    sl_hits = [t for t in closed_trades if t["exit_reason"] == "STOP LOSS"]

    closed_with_points = [t for t in closed_trades if t["points"] is not None]
    wins = [t for t in closed_with_points if t["points"] > 0]
    win_rate = round(len(wins) / len(closed_with_points) * 100, 1) if closed_with_points else None
    net_pnl = round(sum(t["points"] for t in closed_with_points), 2) if closed_with_points else None

    print(f"Signals generated today: {len(signals_today)} (actionable BUY CE/PE: {len(actionable)})")
    print(f"Trades opened today: {len(opened_today)}")
    print(f"Active trades: {len(open_trades)}")
    print(f"Target hits: {len(target_hits)}")
    print(f"Stop-loss hits: {len(sl_hits)}")
    print(f"Win rate: {win_rate}%" if win_rate is not None else "Win rate: NO DATA FOUND (no closed trades)")
    print(f"Net P&L: {net_pnl:+.2f} pts" if net_pnl is not None else "Net P&L: NO DATA FOUND (no closed trades)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run-cycle",
        help="run one AI Trading Engine cycle across every watched symbol -- THE ONLY WRITE COMMAND. "
             "Opens a real ti_paper_trades row for any actionable BUY CE/PE recommendation. "
             "Does not touch RUNTIME_SCHEDULER_ENABLED or NEVER_SCHEDULABLE_AGENTS -- this is a "
             "separate, manual-only entrypoint; nothing here recurs or runs unattended.",
    )
    p_run.set_defaults(func=_cmd_run_cycle)

    p_today = sub.add_parser("today", help="read-only: today's ti_paper_trades rows (open + closed), with live unrealized P&L for open ones")
    p_today.set_defaults(func=_cmd_today)

    p_audit = sub.add_parser("audit", help="read-only: today's signals/activations/target-hits/SL-hits/win-rate/net-P&L summary")
    p_audit.set_defaults(func=_cmd_audit)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

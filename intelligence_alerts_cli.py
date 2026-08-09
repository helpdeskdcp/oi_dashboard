"""
intelligence_alerts_cli.py -- Milestone 14, Phase 1: Intelligence
Alerting Layer. Manual operator CLI for agents.intelligence_alerts --
the ONLY way a rule is ever evaluated, recorded to
intelligence_alerts_log, or delivered via Telegram/email in this
codebase. No scheduler wiring, no background thread, no HTTP write
route. Mirrors intelligence_history_cli.py's own established pattern
exactly.

Deliberately does NOT import app.py to reach send_telegram() -- doing so
would trigger app.py's own module-level Angel One SmartAPI login side
effect (confirmed during this milestone's own live verification work
against a throwaway server). Instead this script implements a small,
standalone Telegram sender reading the exact same TELEGRAM_BOT_TOKEN/
TELEGRAM_CHAT_ID env vars app.py already uses for its own notifications.
auth.py IS imported directly for send_email() -- it has no comparable
import-time side effect.

Usage:
    python3 intelligence_alerts_cli.py check NIFTY [--dry-run]
        # evaluates all four rules against already-logged
        # intelligence_snapshots_log history for NIFTY. --dry-run prints
        # what WOULD trigger, performs ZERO database writes and ZERO
        # Telegram/email sends. Without --dry-run, each triggered rule
        # is recorded to intelligence_alerts_log and a best-effort
        # Telegram/email send is attempted.

    python3 intelligence_alerts_cli.py recent [--symbol NIFTY] [--limit 10] [--offset 0]
        # read-only: paginated alert history.

    python3 intelligence_alerts_cli.py rules
        # read-only: the active threshold config (agents/config.py).

    python3 intelligence_alerts_cli.py status
        # read-only: the same payload GET /api/intelligence/alerts/status
        # returns.

Every write this script can ever perform (outside --dry-run) lands in
intelligence_alerts_log -- and nowhere else. No broker module, no
paper_orders/paper_trades table, no scheduler, no runtime-control flag,
no order of any kind is ever touched. --dry-run performs no database
write and no delivery attempt of any kind.
"""
import argparse
import datetime as dt
import os

import requests

import auth
from agents import config as agents_config
from agents.intelligence_alerts import api as alerts_api
from agents.intelligence_alerts import rules
from agents.intelligence_alerts import store as alerts_store

DRY_RUN_BANNER = "DRY RUN — NO DATABASE WRITES, NO TELEGRAM/EMAIL SENDS PERFORMED"


def _send_telegram(msg: str) -> bool:
    """Returns True only if a send was ATTEMPTED (both env vars
    configured and the request didn't raise) -- Telegram's response body
    isn't checked, matching app.py's own send_telegram()'s behavior, so
    this is not a delivery confirmation."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg}, timeout=5,
        )
        return True
    except Exception:
        return False


def _cmd_check(args):
    triggered = rules.evaluate_all(symbol=args.symbol)
    if not triggered:
        print(f"{args.symbol}: no alert conditions triggered.")
        if args.dry_run:
            print()
            print(DRY_RUN_BANNER)
        return

    if args.dry_run:
        print(f"{args.symbol}: DRY RUN -- {len(triggered)} rule(s) would trigger")
        for r in triggered:
            print(f"  [{r['rule']}] {r['detail']}")
        print()
        print(DRY_RUN_BANNER)
        return

    now = dt.datetime.now().isoformat()
    for r in triggered:
        msg = f"[Intelligence Alert] {r['detail']}"
        delivered_telegram = _send_telegram(msg)
        delivered_email = False
        if agents_config.INTELLIGENCE_ALERT_EMAIL_TO:
            delivered_email = auth.send_email(
                agents_config.INTELLIGENCE_ALERT_EMAIL_TO, f"Intelligence Alert: {r['rule']}", msg
            )
        row_id = alerts_store.record_alert(
            ts=now, symbol=args.symbol, rule=r["rule"], detail=r["detail"],
            delivered_telegram=delivered_telegram, delivered_email=delivered_email,
        )
        print(f"{args.symbol}: [{r['rule']}] logged id={row_id} "
              f"(telegram={'attempted' if delivered_telegram else 'skipped'}, "
              f"email={'sent' if delivered_email else 'skipped'})")
        print(f"  {r['detail']}")


def _cmd_recent(args):
    page = alerts_api.get_recent_page(symbol=args.symbol, limit=args.limit, offset=args.offset)
    print(f"Intelligence alerts ({page['offset']}-{page['offset'] + len(page['items'])} of {page['total']}):")
    for row in page["items"]:
        print(f"  [{row['ts']}] {row['symbol']} {row['rule']}: {row['detail']}")


def _cmd_rules(args):
    for rule, config in alerts_api.get_rules().items():
        print(f"{rule}: {config}")


def _cmd_status(args):
    for key, value in alerts_api.get_status().items():
        print(f"{key}: {value}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="evaluate all rules for a symbol against already-logged history")
    p_check.add_argument("symbol")
    p_check.add_argument("--dry-run", action="store_true", help="evaluate and display only -- zero writes, zero sends")
    p_check.set_defaults(func=_cmd_check)

    p_recent = sub.add_parser("recent", help="paginated alert history")
    p_recent.add_argument("--symbol", default=None)
    p_recent.add_argument("--limit", type=int, default=10)
    p_recent.add_argument("--offset", type=int, default=0)
    p_recent.set_defaults(func=_cmd_recent)

    p_rules = sub.add_parser("rules", help="active threshold config")
    p_rules.set_defaults(func=_cmd_rules)

    p_status = sub.add_parser("status", help="read-only intelligence alerts status")
    p_status.set_defaults(func=_cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

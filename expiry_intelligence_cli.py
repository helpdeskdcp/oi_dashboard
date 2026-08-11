#!/usr/bin/env python3
"""
expiry_intelligence_cli.py -- Milestone 17+: read-only operator CLI for
expiry_intelligence.py. Prints live, calendar-independent expiry status
(next expiry, weekly/monthly, expiry-today flag, data source used) for
every configured NSE/BSE index, plus the cross-index global flags
(today/tomorrow expiry lists, monthly-expiry-week, high-gamma-day).

No weekday is ever assumed anywhere in this tool or in expiry_intelligence.py
itself (no "NIFTY=Thursday"-style table) -- every date printed here comes
straight from whatever Angel One's own instrument master currently lists.

Usage:
    python3 expiry_intelligence_cli.py
        # table view, using the instrument master's normal <24h file cache
        # (same cache app.py's own live loop uses).

    python3 expiry_intelligence_cli.py --live
        # forces a fresh instrument-master download first, bypassing the
        # <24h cache -- use right after NSE/BSE/MCX publish a new expiry
        # and you don't want to wait for the next scheduled refresh.

    python3 expiry_intelligence_cli.py --json
        # machine-readable output instead of a table.

    python3 expiry_intelligence_cli.py --index NIFTY --index SENSEX
        # restrict to specific index(es) instead of every configured one.

Never touches a live broker session: builds a read-only AngelOneFetcher
that skips AngelOneFetcher.__init__'s own self._login() call entirely
(via __new__, see _build_readonly_fetcher() below) -- only
_load_instrument_master() ever runs, which only reads/writes the local
JSON cache file or the public (unauthenticated) Angel One margin-
calculator URL. Safe to run alongside an already-running dashboard
process without risking a duplicate SmartAPI session.
"""
import argparse
import datetime as dt
import json
import os
import sys

os.environ.setdefault("SKIP_AUTOSTART", "1")   # must be set before importing app -- see app.py's own
                                                 # "Startup -- runs on import" block; without this,
                                                 # importing app.py would open a REAL broker session.

import app as _app  # noqa: E402
import expiry_intelligence  # noqa: E402


def _build_readonly_fetcher(force_refresh=False):
    """A read-only AngelOneFetcher: instrument master loaded, broker
    session never opened. Bypasses __init__ (which calls self._login())
    via __new__ -- _load_instrument_master() only touches self.instruments
    and the module-level cache file/URL, nothing else on the instance."""
    fetcher = _app.AngelOneFetcher.__new__(_app.AngelOneFetcher)
    fetcher.instruments = []
    fetcher._load_instrument_master(force_refresh=force_refresh)
    return fetcher


def _default_indexes():
    return {s: cfg["exch"] for s, cfg in _app.SYMBOLS.items() if cfg["type"] == "index_option"}


def _print_table(flags: dict, global_context: dict):
    today = expiry_intelligence._today_ist()
    print(f"Expiry Intelligence -- as of {today.isoformat()} (Asia/Kolkata)\n")
    header = f"{'INDEX':<14}{'NEXT EXPIRY':<14}{'DAYS':<6}{'TODAY?':<8}{'TYPE':<10}{'SOURCE'}"
    print(header)
    print("-" * len(header))
    for idx, status in flags.items():
        if "error" in status:
            print(f"{idx:<14}{'--':<14}{'--':<6}{'--':<8}{'--':<10}ERROR: {status['error']}")
            continue
        print(
            f"{idx:<14}{status['next_expiry'].isoformat():<14}{status['days_to_expiry']:<6}"
            f"{'YES' if status['expiry_today'] else 'NO':<8}{status['weekly_or_monthly']:<10}{status['source']}"
        )
    print()
    print(f"today_expiry_indexes:    {global_context['today_expiry_indexes']}")
    print(f"tomorrow_expiry_indexes: {global_context['tomorrow_expiry_indexes']}")
    print(f"monthly_expiry_week:     {global_context['monthly_expiry_week']}")
    print(f"high_gamma_day:          {global_context['high_gamma_day']}")


def _json_default(o):
    if isinstance(o, dt.date):
        return o.isoformat()
    raise TypeError(f"not JSON-serializable: {o!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                         help="force a fresh instrument-master download instead of reusing the <24h cache")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a table")
    parser.add_argument("--index", action="append", dest="indexes", default=None,
                         help="restrict to a specific index (repeatable). Default: every index_option "
                              "symbol in app.py's SYMBOLS.")
    args = parser.parse_args()

    fetcher = _build_readonly_fetcher(force_refresh=args.live)
    if not fetcher.instruments:
        print("ERROR: instrument master could not be loaded (see log above) -- nothing to report.",
              file=sys.stderr)
        raise SystemExit(1)

    indexes = (
        {idx: _app.SYMBOLS.get(idx, {}).get("exch") for idx in args.indexes}
        if args.indexes else _default_indexes()
    )

    flags = expiry_intelligence.get_all_index_expiry_flags(fetcher, indexes=indexes)
    global_context = expiry_intelligence.global_context_from_flags(flags)

    if args.json:
        print(json.dumps({**global_context, "indexes": flags}, indent=2, default=_json_default))
    else:
        _print_table(flags, global_context)


if __name__ == "__main__":
    main()

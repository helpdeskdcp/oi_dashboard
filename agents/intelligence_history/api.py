"""
agents/intelligence_history/api.py -- Milestone 13, Phase 2: read-only
aggregation functions backing the three GET-only
/api/intelligence/history/* routes in app.py. Every function here only
reads (store.py's own SELECT-only helpers + analytics.compute_report(),
itself read-only) -- nothing in this module writes anything, matching
"no POST/PUT/PATCH/DELETE endpoint in this phase."
"""
import datetime as dt

from . import analytics, store


def _start_of_today_iso() -> str:
    """Same naive-datetime, server-local-time convention
    intelligence_history_cli.py's own record_snapshot() calls already
    use (dt.datetime.now().isoformat())."""
    return dt.datetime.combine(dt.date.today(), dt.time.min).isoformat()


def get_status() -> dict:
    today_start = _start_of_today_iso()
    return {
        "mode": "intelligence_history",
        "read_only": True,
        "no_orders_placed": True,
        "snapshot_count": store.count_total(),
        "last_snapshot_ts": store.last_snapshot_ts(),
        "snapshots_today": store.count_since(today_start),
    }


def get_recent(*, symbol: str | None = None, limit: int = 10) -> list:
    return store.list_recent(symbol=symbol, limit=limit)


def get_report(*, symbol: str, since_ts: str | None = None) -> dict:
    return analytics.compute_report(symbol=symbol, since_ts=since_ts)

"""
agents/intelligence_history/api.py -- Milestone 13, Phase 2: read-only
aggregation functions backing the three GET-only
/api/intelligence/history/* routes in app.py. Every function here only
reads (store.py's own SELECT-only helpers + analytics.compute_report(),
itself read-only) -- nothing in this module writes anything, matching
"no POST/PUT/PATCH/DELETE endpoint in this phase."
"""
import datetime as dt

from agents import config as agents_config

from . import analytics, store


def _start_of_today_iso() -> str:
    """Same naive-datetime, server-local-time convention
    intelligence_history_cli.py's own record_snapshot() calls already
    use (dt.datetime.now().isoformat())."""
    return dt.datetime.combine(dt.date.today(), dt.time.min).isoformat()


def get_status() -> dict:
    """Milestone 13, Phase 3: extended with the fields the dashboard's
    Runtime Status card needs. `last_manual_snapshot_ts` and
    `last_history_write_ts` are intentionally the same value --
    intelligence_history_cli.py's `log` command is the only way a
    snapshot is ever both taken and recorded, in one action, so there is
    no separate "queried but not logged" timestamp to report honestly.
    `runtime_scheduler_enabled` reads the real flag (agents.config);
    it is never true as a side effect of this module."""
    today_start = _start_of_today_iso()
    last_ts = store.last_snapshot_ts()
    total = store.count_total()
    return {
        "mode": "intelligence_history",
        "read_only": True,
        "no_orders_placed": True,
        "snapshot_count": total,
        "last_snapshot_ts": last_ts,
        "snapshots_today": store.count_since(today_start),
        "runtime_scheduler_enabled": agents_config.RUNTIME_SCHEDULER_ENABLED,
        "last_manual_snapshot_ts": last_ts,
        "last_history_write_ts": last_ts,
        "total_history_records": total,
        "app_version": agents_config.APP_VERSION,
        "environment": agents_config.ENVIRONMENT,
    }


def get_recent(*, symbol: str | None = None, limit: int = 10, offset: int = 0) -> list:
    return store.list_recent(symbol=symbol, limit=limit, offset=offset)


def get_recent_page(*, symbol: str | None = None, limit: int = 10, offset: int = 0) -> dict:
    """Milestone 13, Phase 3: paginated wrapper for the dashboard's
    history table -- `get_recent` above stays a bare list (unchanged,
    already covered by existing tests); this adds the `total` count a
    page-through UI needs, respecting the same symbol filter."""
    return {
        "items": store.list_recent(symbol=symbol, limit=limit, offset=offset),
        "total": store.count_total(symbol=symbol),
        "limit": limit,
        "offset": offset,
    }


def get_snapshot(snapshot_id: int) -> dict | None:
    return store.get_by_id(snapshot_id)


def get_report(*, symbol: str, since_ts: str | None = None) -> dict:
    return analytics.compute_report(symbol=symbol, since_ts=since_ts)

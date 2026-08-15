"""
agents/trading_intelligence/monitoring_center.py -- Milestone 21, Phase 2:
the Autonomous Trade Control Center's own read-only aggregation layer.

Every read here reuses already-computed/already-stored data --
intelligence_orchestrator.build_snapshot() (a pure, on-demand read over
market_data.get_snapshot()/data_access), virtual_trailing.list_states()
(this package's own advisory table), ti_store.list_open_trades() (Milestone
21 Phase 2 data-integrity audit fix, see _enrich_trade()'s own docstring
-- a plain read, reconciling virtual_trailing_state against ti_paper_trades'
own status, never a write to either), and
agents.sys_admin.sysadmin_store.get_agent_status() (the runtime's own
execution bookkeeping). No new broker call, no new polling loop -- the
"Refresh Snapshot" button and any auto-refresh timer in the dashboard
just re-fetch the same already-cheap read this module already does.

The two mutating entry points here (pause_monitoring()/resume_monitoring()/
reset_trade()) are thin pass-throughs to virtual_trailing.py's own
control functions -- this module owns no table of its own. Neither ever
writes to ti_store.ti_paper_trades or touches a broker; they only affect
the Virtual Trailing Engine's own shadow state, per virtual_trailing.py's
own docstring. get_control_center_snapshot() reads ti_paper_trades (never
writes it) purely to detect a real trade that has already closed.
"""
import intelligence_orchestrator

from . import ti_store, virtual_trailing
from ..sys_admin import sysadmin_store


def pause_monitoring() -> None:
    virtual_trailing.set_paused(True)


def resume_monitoring() -> None:
    virtual_trailing.set_paused(False)


def reset_trade(trade_id: int) -> bool:
    return virtual_trailing.reset_state(trade_id)


def _intel_card(symbol: str | None) -> dict | None:
    if not symbol:
        return None
    try:
        snapshot = intelligence_orchestrator.build_snapshot(symbol)
    except Exception:
        return None
    return snapshot.to_dict() if snapshot else None


def _enrich_trade(row: dict, *, open_trade_ids: set) -> dict:
    """Milestone 21 Phase 2 data-integrity audit fix: virtual_trailing.py's
    own run_virtual_trailing_cycle() only ever evaluates trades still
    present in ti_store.list_open_trades() -- once a real trade closes, it
    drops out of that list, the cycle stops touching that trade_id
    forever, and the row's own `state` field is frozen at whatever it was
    (often still "ACTIVE", if the virtual SL hadn't independently been hit
    yet). That freezing is virtual_trailing.py's own intentional, tested
    behavior (see its own module docstring) and is NOT changed here.

    What WAS missing is any reconciliation against ti_store's own source
    of truth for "is this real trade still open" -- added here, at READ
    time only, never persisted back onto virtual_trailing_state (still
    zero writes to that table, zero writes to a real trade, same
    read-only contract this whole module already has). `open_trade_ids`
    is computed once per snapshot request (cheap -- a single already-
    indexed ti_paper_trades query), not per row.

    A row is `orphaned` when it still claims state=="ACTIVE" but its real
    trade_id is no longer open -- nothing about the row's own stored
    fields (highest_premium/virtual_sl/locked_profit) is touched or
    hidden; it's only tagged so callers (get_control_center_snapshot()'s
    own aggregates, the dashboard's trade grid) can stop treating a dead
    position as if it were still being live-tracked."""
    orphaned = row["state"] == "ACTIVE" and row["trade_id"] not in open_trade_ids
    return {**row, "current_premium": virtual_trailing.current_premium(row), "orphaned": orphaned}


def get_control_center_snapshot(*, symbol: str | None = None) -> dict:
    """The Control Center's full payload -- the cards plus the trade
    grid. `symbol` scopes the AI Bias/Confidence/Institutional Score
    card only (those are inherently per-symbol); every other card and
    the trade grid are portfolio-wide, across every symbol this engine
    has ever tracked a trade for.

    `active_auto_paper_trades`/`total_locked_profit` now exclude orphaned
    rows (see _enrich_trade()'s own docstring) -- a dead position no
    longer inflates these two aggregates. `highest_premium_captured`
    deliberately still considers every row (it's a portfolio-wide
    historical high-water mark, not a "currently active" stat, and was
    never scoped to active_trades even before this fix). `trades` still
    returns every row, orphaned or not -- nothing is hidden, only tagged,
    per this audit's own "fix the source of truth, don't hide rows"
    instruction."""
    intel = _intel_card(symbol)
    trades = virtual_trailing.list_states()
    open_trade_ids = {t["id"] for t in ti_store.list_open_trades()}
    enriched = [_enrich_trade(t, open_trade_ids=open_trade_ids) for t in trades]
    active_trades = [t for t in enriched if t["state"] == "ACTIVE" and not t["orphaned"]]

    stage_counts = {}
    for t in enriched:
        label = "ORPHANED" if t["orphaned"] else t["stage"]
        stage_counts[label] = stage_counts.get(label, 0) + 1

    scheduler_health = sysadmin_store.get_agent_status("trading_intelligence")

    return {
        "symbol": symbol,
        "ai_bias": intel["bias"] if intel else None,
        "ai_confidence": intel["confidence"] if intel else None,
        "institutional_score": intel["institutional_score"] if intel else None,
        "active_auto_paper_trades": len(active_trades),
        "total_locked_profit": round(sum(t["locked_profit"] for t in active_trades), 2),
        "highest_premium_captured": round(max((t["highest_premium"] for t in trades), default=0.0), 2),
        "virtual_trailing_status": stage_counts,
        "scheduler_health": scheduler_health,
        "monitoring_paused": virtual_trailing.is_paused(),
        "trades": enriched,
    }

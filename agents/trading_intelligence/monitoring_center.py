"""
agents/trading_intelligence/monitoring_center.py -- Milestone 21, Phase 2:
the Autonomous Trade Control Center's own read-only aggregation layer.

Every read here reuses already-computed/already-stored data --
intelligence_orchestrator.build_snapshot() (a pure, on-demand read over
market_data.get_snapshot()/data_access), virtual_trailing.list_states()
(this package's own advisory table), and
agents.sys_admin.sysadmin_store.get_agent_status() (the runtime's own
execution bookkeeping). No new broker call, no new polling loop -- the
"Refresh Snapshot" button and any auto-refresh timer in the dashboard
just re-fetch the same already-cheap read this module already does.

The two mutating entry points here (pause_monitoring()/resume_monitoring()/
reset_trade()) are thin pass-throughs to virtual_trailing.py's own
control functions -- this module owns no table of its own. Neither ever
touches ti_store.ti_paper_trades or a broker; they only affect the
Virtual Trailing Engine's own shadow state, per virtual_trailing.py's
own docstring.
"""
import intelligence_orchestrator

from . import virtual_trailing
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


def _enrich_trade(row: dict) -> dict:
    return {**row, "current_premium": virtual_trailing.current_premium(row)}


def get_control_center_snapshot(*, symbol: str | None = None) -> dict:
    """The Control Center's full payload -- the cards plus the trade
    grid. `symbol` scopes the AI Bias/Confidence/Institutional Score
    card only (those are inherently per-symbol); every other card and
    the trade grid are portfolio-wide, across every symbol this engine
    has ever tracked a trade for."""
    intel = _intel_card(symbol)
    trades = virtual_trailing.list_states()
    active_trades = [t for t in trades if t["state"] == "ACTIVE"]

    stage_counts = {}
    for t in trades:
        stage_counts[t["stage"]] = stage_counts.get(t["stage"], 0) + 1

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
        "trades": [_enrich_trade(t) for t in trades],
    }

"""
agents/runtime/trading_mode.py -- Milestone 14, Phase 3: the dashboard's
manual PAPER/LIVE ON-OFF switch.

READ THIS BEFORE CHANGING ANYTHING HERE: this module is a LABEL AND
AUDIT TRAIL ONLY. Flipping the mode to LIVE_ENABLED does not, and
cannot, cause a single order to reach Angel One -- there is no code
path anywhere in this repository that calls a broker order-placement
endpoint (verified end-to-end during the Milestone 14 Phase 3 pre-
deployment review: the only SmartConnect(...) instantiation in the
whole checkout is app.py's own AngelOneFetcher, and every method called
on that client is read-only -- ltpData/getMarketData/optionGreek/
getCandleData/position/generateSession -- never placeOrder or any order
variant). agents/runtime/policy_engine.py's own module docstring
documents this exact invariant for the workflow engine's policy
taxonomy ("It does NOT and CANNOT unlock a live broker order, under ANY
policy including 'full_auto'"); this module extends the SAME invariant
to the dashboard's own visible mode badge, rather than letting the two
tell different stories.

Real broker order execution is deliberately NOT built here. It is a
separate, much larger piece of work -- this module's docstring is not
the place to design it, but it would need (at minimum) its own
dedicated, adversarial test suite proving no accidental capital loss is
possible, the same bar self_healing.py's own automatic-recovery lane
was held to in Milestone 8. Widening this file to actually gate a real
order call is exactly the kind of "quiet scope creep" policy_engine.py's
docstring already warns against -- don't do it here without that
review.

Three modes (also the exact log/badge vocabulary the dashboard uses):
- PAPER          -- default. Every boot resets here (see
                     reset_to_paper_on_boot()), regardless of whatever
                     was persisted before a restart.
- LIVE_ENABLED   -- an admin explicitly flipped the switch on. Cosmetic
                     only, see above; every API response for this mode
                     says so explicitly so nobody mistakes the label
                     for real capability.
- LIVE_DISABLED  -- an admin explicitly flipped a previously-LIVE_ENABLED
                     switch back off (distinct from PAPER, which is the
                     boot-default/never-touched state -- this
                     distinction is what the audit trail is for).

Persistence mirrors policy_engine.py/runtime_store.py's own
runtime_policy pattern exactly (single-row upsert table for "current
state" + agents.sys_admin.sysadmin_report for the audit trail) rather
than inventing a second, competing state/audit shape.
"""
from ..sys_admin import sysadmin_report, sysadmin_store
from . import runtime_events, runtime_store

PAPER = "PAPER"
LIVE_ENABLED = "LIVE_ENABLED"
LIVE_DISABLED = "LIVE_DISABLED"

ALL_MODES = (PAPER, LIVE_ENABLED, LIVE_DISABLED)

BADGE = {
    PAPER: {"emoji": "\U0001f7e1", "label": "PAPER MODE"},
    LIVE_ENABLED: {"emoji": "\U0001f7e2", "label": "LIVE MODE"},
    LIVE_DISABLED: {"emoji": "\U0001f534", "label": "LIVE DISABLED"},
}

_AUDIT_MODULE = "trading_mode"


def get_current_mode() -> str:
    override = runtime_store.get_trading_mode_override()
    return override["mode"] if override is not None else PAPER


def get_status() -> dict:
    """Everything the dashboard badge and the GET endpoint need in one
    call: current mode, its badge shape, and whether real execution is
    possible (always False today -- see module docstring)."""
    mode = get_current_mode()
    override = runtime_store.get_trading_mode_override()
    return {
        "mode": mode,
        "badge": BADGE[mode],
        "live_execution_implemented": False,
        "note": "LIVE_ENABLED is a label only -- no code path in this repository can place a real broker order.",
        "changed_by": override["changed_by"] if override else None,
        "reason": override["reason"] if override else None,
        "updated_ts": override["updated_ts"] if override else None,
    }


def set_mode(mode: str, *, changed_by: str, reason: str) -> dict:
    """Records who/why, always -- same discipline policy_engine.set_policy()
    already established (reusing sysadmin_report's shared shape rather
    than a new, competing audit dataclass). Raises ValueError for an
    unknown mode -- a caller-programming error, not a data problem."""
    if mode not in ALL_MODES:
        raise ValueError(f"mode must be one of {ALL_MODES}, got {mode!r}")
    previous_mode = get_current_mode()
    runtime_store.set_trading_mode_override(mode, changed_by=changed_by, reason=reason)
    report = sysadmin_report.build(
        module=_AUDIT_MODULE, action="set_mode",
        reason=f"trading mode changed {previous_mode!r} -> {mode!r} by {changed_by}: {reason}",
        confidence=100,
        evidence={"previous_mode": previous_mode, "new_mode": mode, "changed_by": changed_by, "reason": reason},
        severity="warning" if mode == LIVE_ENABLED else "info",
    )
    sysadmin_store.record_report(report)
    runtime_events.emit_safe(
        _AUDIT_MODULE, runtime_events.TRADING_MODE_CHANGED,
        {"previous_mode": previous_mode, "new_mode": mode, "changed_by": changed_by, "reason": reason},
    )
    return get_status()


def reset_to_paper_on_boot() -> None:
    """Called once at app startup (app.py), unconditionally -- the
    "system must always start in PAPER MODE after reboot" safety rule.
    Always writes a fresh audit entry too, even when the mode was
    already PAPER, so the audit trail shows every boot, not just the
    boots that changed something."""
    previous_mode = get_current_mode()
    set_mode(PAPER, changed_by="system", reason="startup safety reset -- always boots to PAPER MODE")
    return previous_mode


def audit_history(*, limit: int = 20) -> list:
    """Every toggle ever recorded, newest first -- timestamp/admin/
    previous mode/new mode, straight from sysadmin_store's own report
    log (see module docstring: no second, parallel audit table)."""
    reports = sysadmin_store.list_reports(module=_AUDIT_MODULE, limit=limit)
    return [
        {
            "ts": r["ts"], "changed_by": r["report_json"]["evidence"].get("changed_by"),
            "previous_mode": r["report_json"]["evidence"].get("previous_mode"),
            "new_mode": r["report_json"]["evidence"].get("new_mode"),
            "reason": r["report_json"]["evidence"].get("reason"),
        }
        for r in reports
    ]

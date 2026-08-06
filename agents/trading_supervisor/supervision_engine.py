"""
agents/trading_supervisor/supervision_engine.py -- combines market
state, conflicting-signal detection, and "did the risk gate actually run
and pass" into one explainable SupervisionVerdict. Gate 7 of the
promotion pipeline (agents/trading_supervisor/gate.py wraps this into a
GateResult, appended after agents.risk_manager.gate's Gate 6).

"Trigger alerts instead of automatic execution when uncertainty is
high": unresolved uncertainty (an unknown market-state dimension, a
detected conflict, elevated volatility/expiry/event risk, a stale feed)
always lands on REQUIRES_REVIEW, never a silent APPROVED -- the same
posture agents.risk_manager.risk_intelligence already holds for a
candidate numerically close to a known failure.

Defense in depth: this module re-checks that Gate 6 (risk_assessment)
actually appears in gate_results and did not FAIL, rather than trusting
that it ran just because verify() was called -- the same "never trust
upstream intent, always re-verify the actual artifact" principle
agents/dev_agent/patcher.py's self-modification guard already holds
itself to.
"""
import dataclasses
import datetime as dt

from . import conflict_detector, data_health, market_state


@dataclasses.dataclass
class SupervisionVerdict:
    decision: str  # "APPROVED" | "REQUIRES_REVIEW" | "REJECTED"
    explanation: str
    risk_gate_status: str  # "passed" | "failed" | "missing"
    market_state: dict
    conflicts: list
    data_health: dict


def _risk_gate_status(gate_results: list) -> str:
    risk_gate = next((g for g in gate_results if g.gate == "risk_assessment"), None)
    if risk_gate is None:
        return "missing"
    return "failed" if risk_gate.status.value == "failed" else "passed"


def verify(*, candidate_name: str, symbol: str, direction: str, strategy_family: str,
           gate_results: list, memory_store, date: str | None = None,
           expiry_dates: set | None = None, event_dates: set | None = None) -> SupervisionVerdict:
    date = date or dt.date.today().isoformat()
    risk_status = _risk_gate_status(gate_results)
    state = market_state.assess(symbol, date, expiry_dates=expiry_dates, event_dates=event_dates)
    active = conflict_detector.active_strategy_directions(memory_store, exclude_strategy_name=strategy_family)
    conflicts = conflict_detector.detect_conflicts(candidate_name, symbol, direction, active)
    feed = data_health.check_feed_staleness(symbol, as_of_date=date)

    reasons = []
    if risk_status != "passed":
        reasons.append(
            f"risk gate status is {risk_status!r} -- a trading recommendation must clear risk "
            f"approval before this supervisor will ever approve it."
        )
        decision = "REJECTED"
    else:
        concerns = []
        if conflicts:
            names = ", ".join(f"{c.strategy_b} ({c.direction_b})" for c in conflicts)
            concerns.append(f"{len(conflicts)} conflicting signal(s) on {symbol}: {names}.")
        if state.is_elevated_uncertainty:
            concerns.append(
                f"elevated market uncertainty on {symbol}: volatility={state.volatility.get('level')}, "
                f"expiry={state.expiry.get('status')}, event={state.event.get('status')}."
            )
        if state.has_unknowns:
            concerns.append("one or more market-state dimensions could not be determined.")
        if feed.is_stale:
            concerns.append(f"data feed for {symbol} looks stale: {feed.note}")

        if concerns:
            reasons.extend(concerns)
            decision = "REQUIRES_REVIEW"
        else:
            reasons.append("risk approval confirmed, no conflicting signals, market state normal, feed current.")
            decision = "APPROVED"

    explanation = (
        f"Trading Supervisor: {decision} for {candidate_name} ({symbol}, {direction}). " + " ".join(reasons)
    )

    return SupervisionVerdict(
        decision=decision, explanation=explanation, risk_gate_status=risk_status,
        market_state=dataclasses.asdict(state), conflicts=[dataclasses.asdict(c) for c in conflicts],
        data_health=dataclasses.asdict(feed),
    )

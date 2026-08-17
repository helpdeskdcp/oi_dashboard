"""
agents/trading_intelligence/signal_graph.py -- Post-launch upgrade,
Phase 2: a minimal LangGraph shadow graph.

SCOPE (deliberately narrow, per the approved Phase 2 plan): wrap the
already-existing, already-tested detection/scoring functions
(market_data.get_snapshot, institutional_intelligence.analyze,
regime_profile.classify, timeframe_confirmation.check,
ai_trading_engine.evaluate) as explicit LangGraph nodes, so their
individual outputs and per-node latency become independently observable
and loggable. This phase adds NO new decision logic, NO memory/pattern
retrieval, NO Obsidian dependency, and NO LLM call of any kind -- every
node here is a deterministic Python function calling a real, existing
module. The graph's own action/confidence therefore comes straight from
the SAME ai_trading_engine.evaluate() call the real engine already makes
this cycle (reusing the caller's already-computed snapshot/findings/
recommendation when given, exactly like every other dedup convention in
this package) -- proving the orchestration plumbing (state passing,
per-node latency, failure isolation, persistence) works BEFORE any
future phase introduces genuinely independent signal logic (historical
pattern retrieval, Obsidian memory) that could actually diverge from
today's engine.

SHADOW ONLY: run_shadow() never raises to its caller (matches
telegram_notifier.py's own never-raise, fire-and-forget contract) and
its result is written to signal_graph_store's own table -- never read
back by paper_trading.enter_from_recommendation() or telegram_notifier.
A LangGraph failure, a node exception, or a missing optional dependency
must never affect the real trading-intelligence cycle; see
_run_node()'s own docstring for how each node is individually isolated
so one node's failure doesn't take down the rest of the graph's
observability value for that cycle.

The LLM is not used anywhere in this module. Nothing here calls
agents.llm_providers. Every field in GraphState comes from a real,
already-computed application value -- never invented.
"""
import logging
import time

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from . import ai_trading_engine, institutional_intelligence, market_data, regime_profile, timeframe_confirmation
from . import signal_graph_store

log = logging.getLogger(__name__)


class GraphState(TypedDict, total=False):
    symbol: str
    expiry_date: object
    snapshot: object
    findings: list
    data_available: bool
    regime_trend: str | None
    regime_volatility: str | None
    recommendation: object
    timeframe_alignment_score: float | None
    timeframe_alignment_label: str | None
    node_latencies: dict
    node_errors: dict


def _run_node(name, fn):
    """Wraps one node function so it can NEVER stop the graph from
    reaching END: any exception is caught, recorded under
    state["node_errors"][name] (kept, not swallowed silently -- a future
    reader of ti_signal_graph_shadow should be able to see exactly which
    node failed and why), and the graph proceeds with whatever state
    already exists. Latency is recorded whether the node succeeded or
    failed, per Section 13's "log each node latency" requirement."""
    def wrapped(state: GraphState) -> GraphState:
        node_latencies = dict(state.get("node_latencies") or {})
        node_errors = dict(state.get("node_errors") or {})
        start = time.monotonic()
        try:
            updates = fn(state) or {}
        except Exception as e:
            node_errors[name] = str(e)
            updates = {}
            log.warning(f"signal_graph node {name!r} failed (isolated, graph continues): {e}")
        node_latencies[name] = round((time.monotonic() - start) * 1000, 2)
        return {**updates, "node_latencies": node_latencies, "node_errors": node_errors}
    return wrapped


def _n_fetch_market_state(state: GraphState) -> dict:
    if state.get("snapshot") is not None:
        snapshot = state["snapshot"]
    else:
        snapshot = market_data.get_snapshot(state["symbol"], expiry_date=state.get("expiry_date"))
    return {"snapshot": snapshot, "data_available": bool(snapshot and snapshot.available)}


def _n_analyze_institutional(state: GraphState) -> dict:
    if not state.get("data_available"):
        return {"findings": []}
    if state.get("findings") is not None:
        return {"findings": state["findings"]}
    ii = institutional_intelligence.analyze(
        state["symbol"], snapshot=state["snapshot"], expiry_date=state.get("expiry_date"))
    return {"findings": ii.get("findings", [])}


def _n_detect_regime(state: GraphState) -> dict:
    if not state.get("data_available"):
        return {"regime_trend": None, "regime_volatility": None}
    regime = regime_profile.classify(state["symbol"], snapshot=state["snapshot"])
    return {"regime_trend": regime.trend_regime, "regime_volatility": regime.volatility_regime}


def _n_score_and_decide(state: GraphState) -> dict:
    if state.get("recommendation") is not None:
        return {"recommendation": state["recommendation"]}
    rec = ai_trading_engine.evaluate(
        state["symbol"], snapshot=state.get("snapshot"), findings=state.get("findings"),
        expiry_date=state.get("expiry_date"),
    )
    return {"recommendation": rec}


def _n_confirm_timeframe(state: GraphState) -> dict:
    rec = state.get("recommendation")
    direction = getattr(rec, "direction", None)
    if direction not in ("CE", "PE"):
        return {"timeframe_alignment_score": None, "timeframe_alignment_label": None}
    alignment = timeframe_confirmation.check(state["symbol"], direction=direction)
    return {"timeframe_alignment_score": alignment.alignment_score, "timeframe_alignment_label": alignment.alignment_label}


_GRAPH = None


def _build_graph():
    """Built once, module-level cache -- compiling a StateGraph has real
    (small) overhead, and the graph's structure never changes at
    runtime, the same "compile once, invoke many times" pattern
    langgraph's own docs recommend."""
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH
    graph = StateGraph(GraphState)
    graph.add_node("fetch_market_state", _run_node("fetch_market_state", _n_fetch_market_state))
    graph.add_node("analyze_institutional", _run_node("analyze_institutional", _n_analyze_institutional))
    graph.add_node("detect_regime", _run_node("detect_regime", _n_detect_regime))
    graph.add_node("score_and_decide", _run_node("score_and_decide", _n_score_and_decide))
    graph.add_node("confirm_timeframe", _run_node("confirm_timeframe", _n_confirm_timeframe))
    graph.set_entry_point("fetch_market_state")
    graph.add_edge("fetch_market_state", "analyze_institutional")
    graph.add_edge("analyze_institutional", "detect_regime")
    graph.add_edge("detect_regime", "score_and_decide")
    graph.add_edge("score_and_decide", "confirm_timeframe")
    graph.add_edge("confirm_timeframe", END)
    _GRAPH = graph.compile()
    return _GRAPH


def run_shadow(symbol: str, *, snapshot=None, findings: list | None = None, recommendation=None,
               expiry_date=None, real_engine_action: str | None = None, persist: bool = True) -> dict:
    """Runs the shadow graph for one symbol and returns a plain dict
    (the same shape signal_graph_store.record() writes). Never raises --
    any failure (graph construction, invoke, or an individual node not
    already isolated by _run_node) is caught here and recorded as
    result["error"], matching every other advisory module in this
    package's fire-and-forget contract.

    `snapshot`/`findings`/`recommendation`: pass these when the caller
    (api.run_scheduled_cycle(), the real production cycle) already
    computed them THIS cycle, so the shadow graph observes the exact
    same inputs/decision the real engine used rather than paying for a
    second snapshot/institutional-sweep/evaluate() call -- the same
    dedup discipline every function in this package already follows.
    Standalone callers (tests, a future backtest) leave these None and
    the graph computes its own.

    `real_engine_action`: the real engine's own rec.action for this same
    cycle, if the caller already has it -- stored alongside the graph's
    own action purely for later comparison, never used to influence the
    graph's own output.

    `persist`: writes the result to ti_signal_graph_shadow (default).
    False is only for tests that don't want DB side effects."""
    start = time.monotonic()
    try:
        app_graph = _build_graph()
        final_state = app_graph.invoke({
            "symbol": symbol, "expiry_date": expiry_date, "snapshot": snapshot,
            "findings": findings, "recommendation": recommendation,
        })
        rec = final_state.get("recommendation")
        graph_action = getattr(rec, "action", None)
        result = {
            "symbol": symbol,
            "data_available": final_state.get("data_available", False),
            "regime_trend": final_state.get("regime_trend"),
            "regime_volatility": final_state.get("regime_volatility"),
            "institutional_finding_count": len(final_state.get("findings") or []),
            "graph_action": graph_action,
            "graph_direction": getattr(rec, "direction", None),
            "graph_confidence": getattr(rec, "confidence", None),
            "timeframe_alignment_score": final_state.get("timeframe_alignment_score"),
            "timeframe_alignment_label": final_state.get("timeframe_alignment_label"),
            "real_engine_action": real_engine_action,
            "agrees_with_real_engine": (
                None if real_engine_action is None else (graph_action == real_engine_action)
            ),
            "node_latencies": final_state.get("node_latencies") or {},
            "node_errors": final_state.get("node_errors") or {},
            "error": None,
        }
    except Exception as e:
        log.warning(f"signal_graph shadow run failed for {symbol!r} (isolated, real engine unaffected): {e}")
        result = {
            "symbol": symbol, "data_available": False, "regime_trend": None, "regime_volatility": None,
            "institutional_finding_count": None, "graph_action": None, "graph_direction": None,
            "graph_confidence": None, "timeframe_alignment_score": None, "timeframe_alignment_label": None,
            "real_engine_action": real_engine_action, "agrees_with_real_engine": None,
            "node_latencies": {}, "node_errors": {}, "error": str(e),
        }
    result["total_latency_ms"] = round((time.monotonic() - start) * 1000, 2)

    if persist:
        try:
            signal_graph_store.record(result)
        except Exception as e:
            log.warning(f"signal_graph_store.record() failed for {symbol!r} (isolated): {e}")

    return result

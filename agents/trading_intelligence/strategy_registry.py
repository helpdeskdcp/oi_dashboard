"""
agents/trading_intelligence/strategy_registry.py -- a single, honest
inventory of every TI_ENABLE_* feature flag in agents/config.py.

Scoped down deliberately from the roadmap's full "Strategy Registry" spec
(Strategy ID/Name/Market/Instrument/Timeframe/Entry conditions/Exit
conditions/Risk rules/Parameters/Version/Status/Backtest+Paper+Live
statistics per strategy): this codebase's actual architecture is a set of
flag-gated engines and shadow layers, each with its own already-documented
scope in agents/config.py's own comments -- not formal per-strategy
entry/exit-condition objects. Inventing that structure here would mean
fabricating data this repo doesn't actually track anywhere. What follows
is the real, existing thing: which engines exist, what module implements
each, a one-line description taken directly from agents/config.py's own
documentation for that flag, and whether it's currently on.

Pure additive read-layer -- reads agents.config's already-resolved boolean
values fresh on every call (never cached, never writes anything, never
gates any decision). Adding a strategy here does not enable it; the
env var in agents/config.py remains the single source of truth.
"""
from .. import config

# (flag name, owning module, one-line description -- copied verbatim in
# substance from agents/config.py's own comment for that flag, so this
# registry can never drift into claiming something the flag doesn't
# actually do).
REGISTRY = [
    {
        "flag": "TI_ENABLE_STRUCTURE_ALERTS",
        "module": "agents/trading_intelligence/structure_alerts.py",
        "description": "Role flips, breakout/breakdown watch, reversal risk -- read-only signal-engine "
                        "evaluation; never opens a trade, never touches the BUY CE/PE decision.",
    },
    {
        "flag": "TI_ENABLE_STRUCTURE_TUNING",
        "module": "agents/trading_intelligence/structure_tuning.py",
        "description": "Bounded/audited adaptive structure-tuning loop -- hard parameter bounds, minimum "
                        "sample size, minimum improvement margin, per-parameter cooldown enforced regardless "
                        "of this flag.",
    },
    {
        "flag": "TI_ENABLE_VIRTUAL_TRAILING",
        "module": "agents/trading_intelligence/virtual_trailing.py",
        "description": "Paper-trade / advisory-only shadow layer tracking a dynamic trailing SL/target "
                        "alongside each open paper trade -- never touches ti_store.close_trade() or any "
                        "broker path.",
    },
    {
        "flag": "TI_ENABLE_SIGNAL_GRAPH_SHADOW",
        "module": "agents/trading_intelligence/signal_graph.py",
        "description": "LangGraph shadow-signal layer -- wraps the SAME detection/scoring functions the "
                        "real engine already calls as explicit graph nodes, writes to ti_signal_graph_shadow, "
                        "never read back by paper_trading.",
    },
    {
        "flag": "TI_ENABLE_TRADE_GUARDIAN_SHADOW",
        "module": "agents/trading_intelligence/trade_guardian.py",
        "description": "Analyzes an already-open, Administrator-entered broker position and RECOMMENDS "
                        "(never places or modifies) a Smart Target/Smart SL.",
    },
    {
        "flag": "TI_ENABLE_EXECUTION_STATE_SHADOW",
        "module": "agents/trading_intelligence/execution_state.py",
        "description": "Wires the SIGNAL/APPROVED/.../COMPLETED state machine to real signal output -- pure "
                        "persistence, no broker call anywhere in this module.",
    },
    {
        "flag": "TI_ENABLE_CONTROL_CENTER_UI",
        "module": "agents/trading_intelligence/monitoring_center.py",
        "description": "Autonomous Trade Control Center dashboard widget -- read-only aggregation; advisory "
                        "pause/resume/reset controls never touch a broker or a real trade.",
    },
    {
        "flag": "TI_ENABLE_AI_LIVE_SNAPSHOT_UI",
        "module": "agents/trading_intelligence/ai_live_snapshot.py",
        "description": "AI Live Analysis Snapshot table -- read-only, reuses already-stored cycle/market-"
                        "structure/candle data, no new broker call or polling loop.",
    },
    {
        "flag": "TI_ENABLE_PERFORMANCE_ANALYTICS_UI",
        "module": "agents/trading_intelligence/performance_analytics.py",
        "description": "Performance Analytics & Strategy Intelligence dashboard widget -- purely read-only "
                        "aggregation over already-stored paper-trade tables.",
    },
    {
        "flag": "TI_ENABLE_EXECUTION_STATE_UI",
        "module": "agents/trading_intelligence/execution_state.py",
        "description": "Read-only admin view over execution_state.py's own list_executions()/"
                        "recent_transitions() -- surfaces already-stored data, changes nothing.",
    },
    {
        "flag": "TI_ENABLE_REGIME_FILTER_SHADOW",
        "module": "agents/trading_intelligence/regime_profile.py",
        "description": "Market-Regime/Chop Detection -- SHADOW ONLY: attaches a regime assessment to every "
                        "Recommendation for observation, never changes action/direction/entry/target/SL.",
    },
    {
        "flag": "TI_ENABLE_MOMENTUM_CONFIRMATION",
        "module": "oi_engine.py (generate_signal)",
        "description": "RSI-exhaustion momentum sub-score -- agreeing direction gets a confidence bonus, "
                        "entering into exhaustion gets a penalty. Any failure is caught and skipped, never raised.",
    },
]


def get_registry() -> list[dict]:
    """Live snapshot -- current enabled/disabled value read fresh from
    agents.config on every call, so this stays honest across a config
    change (e.g. a live .env edit + restart) without needing its own
    code change to match."""
    return [{**entry, "enabled": getattr(config, entry["flag"], False)} for entry in REGISTRY]

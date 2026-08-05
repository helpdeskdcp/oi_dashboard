"""
agents/config.py -- BATI autonomous agents configuration. Every setting
here is env-var-overridable, matching this codebase's existing convention
(see app.py's DB_PATH/DEFAULT_SYMBOL). Nothing in agents/ or
agents/dev_agent/ hardcodes a value that belongs here.
"""
import os

# --- LLM provider selection -------------------------------------------
# Requirement: "must be able to switch providers via configuration only."
# One of agents.llm_providers.available_providers() -- currently "openai",
# "claude", "ollama", "gemini". Nothing outside agents/llm_providers/
# imports a specific provider adapter by name; every caller (starting
# with agents/dev_agent/patcher.py, Milestone 3) goes through
# agents.llm_providers.get_llm_provider(), which reads this value.
AGENT_LLM_PROVIDER = os.getenv("AGENT_LLM_PROVIDER", "openai")


# --- Detection scope / priority -----------------------------------------
# Requirement: "Start with the complete repository. Use priority levels."
# Path-prefix -> tier. Matched by agents/dev_agent/detector.py
# (Milestone 3) via priority_for_path() below; DEFAULT_PRIORITY applies to
# anything not matched (utility scripts, config files, misc).
DETECTION_PRIORITY: dict[int, list[str]] = {
    # Priority 1: Exit Engine / Risk Engine / Position Sizing / Backtest
    # Engine / Trading Core.
    1: [
        "exit_engine_v4.py", "sr_engine_v3.py", "dynamic_sr_engine.py",
        "sr_probability_engine.py", "position_sizing.py", "backtest.py",
        "oi_engine.py", "market_structure.py", "engine_v2.py",
        "candlestick_patterns.py", "scalping_engine.py",
    ],
    # Priority 2: Dashboard / UI / Reports.
    2: ["app.py", "templates/", "static/"],
    # Priority 3: Documentation.
    3: ["README.md", "AUTONOMOUS_AGENTS_ARCHITECTURE.md", "AI_DEVELOPER_AGENT_PLAN.md"],
}
DEFAULT_PRIORITY = 3  # utilities, tests, agents/ (visibility only -- see the hard-block below), everything else


def priority_for_path(path: str) -> int:
    """Longest-matching-prefix wins so a specific file in DETECTION_PRIORITY
    (e.g. "app.py") isn't shadowed by a shorter directory prefix. Falls
    back to DEFAULT_PRIORITY when nothing matches."""
    best_tier, best_len = DEFAULT_PRIORITY, -1
    for tier, prefixes in DETECTION_PRIORITY.items():
        for prefix in prefixes:
            if path == prefix or path.startswith(prefix):
                if len(prefix) > best_len:
                    best_tier, best_len = tier, len(prefix)
    return best_tier


# --- Execution triggers ---------------------------------------------------
# Requirement: "Do not run on a timer... Avoid continuous autonomous
# execution." agents/dev_agent/runner.py (Milestone 4) is invoked BY one
# of these triggers (a CI hook, a git post-commit hook, a manual CLI call,
# a test/backtest-runner catching a failure) -- there is no scheduler/
# cron entry anywhere in this framework.
DEV_AGENT_TRIGGERS = (
    "commit", "pull_request", "manual", "test_failure",
    "backtest_failure", "runtime_exception", "performance_regression",
)


# --- Benchmark baseline -----------------------------------------------------
# Requirement: "Use the existing 3-month NIFTY benchmark as the initial
# baseline... must support multiple symbols and multiple time periods."
# BENCHMARK_BASELINE is what agents/dev_agent/gates/backtest_compare.py
# (Milestone 2) actually runs today; BENCHMARK_AVAILABLE_SYMBOLS is the
# full set the interface is shaped to accept without a signature change
# once each symbol's candle archive/comparison is wired up.
BENCHMARK_BASELINE = {"symbol": "NIFTY", "date_from": "2026-05-01", "date_to": "2026-08-04"}
BENCHMARK_AVAILABLE_SYMBOLS = (
    "NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX",
    "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI",
    "GOLD", "GOLDM", "SILVER", "SILVERM",
)


# --- Regression thresholds ---------------------------------------------------
# Requirement: "Reject any change that decreases profitability, increases
# drawdown, reduces stability, or causes test failures." These are HARD
# reject lines, not "flag for review" thresholds -- 0.0 means ANY
# regression at all rejects the proposal before it reaches
# pending_approval. A future review can loosen these deliberately (e.g. to
# tolerate backtest noise) but the default is zero tolerance, per the
# explicit instruction.
MAX_NET_PNL_REGRESSION_PCT = 0.0
MAX_PROFIT_FACTOR_REGRESSION_PCT = 0.0
MAX_EXPECTANCY_REGRESSION_PCT = 0.0
MAX_DRAWDOWN_INCREASE_PCT = 0.0


# --- Self-modification guard -------------------------------------------------
# Requirement: "Do not implement any self-modifying production code."
# Hard-coded, not configurable -- there is deliberately no env var here.
# agents/dev_agent/detector.py (Milestone 3) refuses any diff touching a
# path under this prefix before a worktree is even created.
SELF_MODIFICATION_GUARD_PREFIX = "agents/"

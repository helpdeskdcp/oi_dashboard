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

# --- Automatic provider fallback (Milestone 3) --------------------------
# Requirement: "Automatic provider fallback." agents.llm_providers.
# generate_with_fallback() tries AGENT_LLM_PROVIDER first, then walks this
# list in order (skipping whichever provider was already tried), and only
# raises once every candidate has failed or is unconfigured. Order reflects
# no ranking judgment -- just a deterministic, config-only fallback chain.
AGENT_LLM_FALLBACK_ORDER = tuple(
    p.strip() for p in os.getenv("AGENT_LLM_FALLBACK_ORDER", "openai,claude,gemini,ollama").split(",")
    if p.strip()
)


# --- AI Memory & Knowledge Base (Milestone 4) ----------------------------
# Requirement: "Use SQLite first, with a clean interface that can later be
# upgraded to PostgreSQL." MEMORY_BACKEND is the ONLY place backend
# selection is decided -- agents.memory.get_memory_store() reads it;
# nothing else in this codebase should read it directly.
MEMORY_BACKEND = os.getenv("MEMORY_BACKEND", "sqlite")
MEMORY_DB_PATH = os.getenv("MEMORY_DB_PATH", "oi_history.db")
# Rows returned per category by agents.memory.context.build_context() --
# kept small so the memory excerpt spliced into an LLM prompt stays a
# short, relevant summary rather than a full table dump.
MEMORY_SEARCH_LIMIT = int(os.getenv("MEMORY_SEARCH_LIMIT", "5"))


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
        # Milestone 5: strategies the AI Quant Researcher promotes land here
        # (repo root, never under agents/ -- self-modification guard would
        # otherwise hard-block every promotion). Listing the directory as
        # Priority 1 means a promoted strategy file's diff DOES trigger the
        # real backtest_compare/benchmark gates -- a genuine regression
        # check against the current production baseline, not a skip, even
        # though the file itself is additive-only.
        "research_strategies/",
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

# Milestone 2 additions -- the regression analyzer tracks nine metrics
# (Net Profit, Profit Factor, Win Rate, Drawdown, Sharpe Ratio, Sortino
# Ratio, Recovery Factor, Expectancy, Trade Count), not just the original
# four. Same zero-tolerance default, same rationale as above.
MAX_WIN_RATE_REGRESSION_PCT = 0.0
MAX_SHARPE_RATIO_REGRESSION_PCT = 0.0
MAX_SORTINO_RATIO_REGRESSION_PCT = 0.0
MAX_RECOVERY_FACTOR_REGRESSION_PCT = 0.0
MAX_TRADE_COUNT_REGRESSION_PCT = 0.0


# --- AI Quant Researcher (Milestone 5) -----------------------------------
# Requirement: "continuously discovers, validates, improves and retires
# trading strategies using mathematical evidence instead of assumptions."
# QUANT_RESEARCH_STRATEGIES_DIR is where a promoted StrategySpec is
# materialized into an actual Python module (agents/quant_researcher/
# codegen.py) -- deliberately at repo root, never under agents/, so it is
# never self-modification-guarded and DOES flow through the ordinary
# five-gate pipeline like any other strategy-file change.
QUANT_RESEARCH_STRATEGIES_DIR = os.getenv("QUANT_RESEARCH_STRATEGIES_DIR", "research_strategies")

# Statistical validation: "Never promote a strategy because of a small
# sample size." A hypothesis's backtest must produce at least this many
# trades before its win/loss record is treated as evidence at all --
# below this, validate() rejects outright regardless of how good the
# stats look, no override.
QUANT_RESEARCH_MIN_TRADES = int(os.getenv("QUANT_RESEARCH_MIN_TRADES", "30"))
# One-sided confidence level (via a normal approximation to the per-trade
# points distribution's mean -- valid for the >=30 sample QUANT_RESEARCH_
# MIN_TRADES already requires, so no scipy dependency is added just for
# this) that mean per-trade P&L is genuinely > 0, not sampling noise.
QUANT_RESEARCH_CONFIDENCE_LEVEL = float(os.getenv("QUANT_RESEARCH_CONFIDENCE_LEVEL", "0.95"))

# Evolution engine: how many parameter combinations optimize_parameters()
# will grid-search per hypothesis per research cycle -- kept small so a
# research cycle stays a research cycle, not an unbounded sweep.
QUANT_RESEARCH_MAX_GRID_COMBINATIONS = int(os.getenv("QUANT_RESEARCH_MAX_GRID_COMBINATIONS", "25"))

# Promotion: symbol/window used to compute the "current production
# strategy" baseline a candidate must beat -- defaults to the same fixed
# scenario BENCHMARK_BASELINE already uses, for the same reason (a stable,
# known-good comparison point), via backtest.simulate_dynamic_sr_v4_trades.
QUANT_RESEARCH_BASELINE = dict(BENCHMARK_BASELINE)


# --- AI Risk Manager (Milestone 6) ---------------------------------------
# Requirement: "Protect capital before profit... every risk decision fully
# explainable and auditable." Every StrategySpec/trade in this codebase is
# already expressed in underlying POINTS, never option-premium currency
# (see backtest.compute_advanced_trade_stats: net_pnl = sum(points), no
# lot-size/premium conversion anywhere) -- position_sizing.compute_quantity's
# own "risk_pct" mode already treats `capital` and per-trade risk
# (abs(entry - initial_sl), i.e. points) as the same implicit unit. The
# Risk Manager keeps that exact convention rather than inventing a
# lot-size/currency model this codebase doesn't otherwise have: every
# *_CAPITAL/*_PNL setting below is in the same points-equivalent unit as
# everything else already computed by backtest.py/agents.quant_researcher.

# Notional account size used for capital-allocation/exposure/heat percentage
# calculations. Deliberately a config constant, not a live broker balance
# lookup -- the Promotion Risk Gate evaluates a candidate BEFORE it's ever
# live, so there is no live account to ask. The Live Portfolio Risk Monitor
# (risk_manager/portfolio_monitor.py) may prefer real capital data where
# the app already tracks it; this constant is its fallback.
RISK_ACCOUNT_CAPITAL = float(os.getenv("RISK_ACCOUNT_CAPITAL", "1000000"))

# Position sizing / capital allocation -- reuses position_sizing.compute_quantity's
# own "risk_pct" semantics: risk this % of RISK_ACCOUNT_CAPITAL on any single
# trade's stop distance.
RISK_MAX_RISK_PER_TRADE_PCT = float(os.getenv("RISK_MAX_RISK_PER_TRADE_PCT", "1.0"))
# Max % of RISK_ACCOUNT_CAPITAL allowed to be deployed (summed risk-per-trade
# across every currently-promoted "is_best" strategy, including the
# candidate) at once.
RISK_MAX_CAPITAL_ALLOCATION_PCT = float(os.getenv("RISK_MAX_CAPITAL_ALLOCATION_PCT", "20.0"))

# Exposure limits, each expressed as % of RISK_ACCOUNT_CAPITAL's allocated
# risk concentrated in one symbol / sector / strategy family.
RISK_MAX_EXPOSURE_PER_SYMBOL_PCT = float(os.getenv("RISK_MAX_EXPOSURE_PER_SYMBOL_PCT", "8.0"))
RISK_MAX_EXPOSURE_PER_SECTOR_PCT = float(os.getenv("RISK_MAX_EXPOSURE_PER_SECTOR_PCT", "12.0"))
RISK_MAX_EXPOSURE_PER_STRATEGY_PCT = float(os.getenv("RISK_MAX_EXPOSURE_PER_STRATEGY_PCT", "10.0"))
RISK_MAX_CONCURRENT_STRATEGIES = int(os.getenv("RISK_MAX_CONCURRENT_STRATEGIES", "10"))

# Symbol -> sector map for RISK_MAX_EXPOSURE_PER_SECTOR_PCT. Covers every
# symbol BENCHMARK_AVAILABLE_SYMBOLS already lists plus the two extra
# symbols with real history archives (data/history/) that aren't in that
# tuple. Unmapped symbols fall back to their own name as a one-symbol
# "sector" (see risk_manager/risk_engine.py's sector_for()) so a brand new
# symbol never crashes exposure math, only under-groups it until this map
# is extended.
RISK_SYMBOL_SECTORS: dict[str, str] = {
    "NIFTY": "index", "BANKNIFTY": "index", "FINNIFTY": "index",
    "SENSEX": "index", "MIDCPNIFTY": "index",
    "INDIA VIX": "volatility",
    "CRUDEOIL": "energy", "CRUDEOILM": "energy", "NATURALGAS": "energy", "NATGASMINI": "energy",
    "GOLD": "metals", "GOLDM": "metals", "SILVER": "metals", "SILVERM": "metals",
}

# Correlation analysis: candidate vs. every other currently-promoted
# strategy's daily-aggregated P&L series. Above this Pearson correlation,
# the pair is flagged (contributes to the risk score; does not by itself
# hard-reject -- see risk_engine.py's compute_risk_score docstring).
RISK_MAX_STRATEGY_CORRELATION = float(os.getenv("RISK_MAX_STRATEGY_CORRELATION", "0.7"))

# VaR / CVaR (Expected Shortfall): historical-simulation method directly on
# the candidate's own realized trade points -- no distributional assumption
# beyond "the trade history observed during backtesting is a representative
# sample," which is exactly the same assumption agents.quant_researcher.
# statistics_validation already leans on for its significance test.
RISK_VAR_CONFIDENCE = float(os.getenv("RISK_VAR_CONFIDENCE", "0.95"))
RISK_CVAR_CONFIDENCE = float(os.getenv("RISK_CVAR_CONFIDENCE", "0.95"))

# Drawdown simulation (bootstrap/Monte Carlo resampling of the realized
# trade sequence -- stdlib random only, no numpy dependency added for this).
RISK_DRAWDOWN_SIMULATION_TRIALS = int(os.getenv("RISK_DRAWDOWN_SIMULATION_TRIALS", "1000"))
RISK_DRAWDOWN_SIMULATION_PERCENTILE = float(os.getenv("RISK_DRAWDOWN_SIMULATION_PERCENTILE", "95"))
RISK_MAX_SIMULATED_DRAWDOWN_PCT = float(os.getenv("RISK_MAX_SIMULATED_DRAWDOWN_PCT", "15.0"))

# Stress testing: "what if every losing trade had been this much worse."
# Expressed as negative fractions -- -0.5 means "losses 50% larger."
RISK_STRESS_TEST_SHOCKS = tuple(
    float(s) for s in os.getenv("RISK_STRESS_TEST_SHOCKS", "-0.25,-0.5,-1.0").split(",") if s.strip()
)

# Risk score (0-100, agents.risk_manager.risk_engine.compute_risk_score) ->
# decision thresholds. Below REJECT is a hard REJECTED, same "no override"
# posture as agents.dev_agent.approval_engine's zero-tolerance regressions;
# between the two thresholds is REQUIRES_REVIEW; at/above REVIEW is APPROVED.
RISK_SCORE_REJECT_BELOW = int(os.getenv("RISK_SCORE_REJECT_BELOW", "40"))
RISK_SCORE_REVIEW_BELOW = int(os.getenv("RISK_SCORE_REVIEW_BELOW", "70"))

# --- Live Portfolio Risk Monitor thresholds -------------------------------
# Same points-equivalent unit as above. "Heat" = sum of open positions'
# entry-to-stop risk / RISK_ACCOUNT_CAPITAL.
RISK_PORTFOLIO_HEAT_LIMIT_PCT = float(os.getenv("RISK_PORTFOLIO_HEAT_LIMIT_PCT", "6.0"))
RISK_DAILY_LOSS_LIMIT_PCT = float(os.getenv("RISK_DAILY_LOSS_LIMIT_PCT", "3.0"))
RISK_MAX_LIVE_DRAWDOWN_PCT = float(os.getenv("RISK_MAX_LIVE_DRAWDOWN_PCT", "15.0"))
RISK_MAX_MARGIN_UTILIZATION_PCT = float(os.getenv("RISK_MAX_MARGIN_UTILIZATION_PCT", "80.0"))
RISK_MAX_CONCENTRATION_PCT = float(os.getenv("RISK_MAX_CONCENTRATION_PCT", "35.0"))

RISK_DB_PATH = os.getenv("RISK_DB_PATH", "oi_history.db")

# AI Risk Intelligence: "never recommend a configuration that previously
# failed without explaining why." Two threshold configs are considered
# "the same configuration" if every shared key is within this percentage
# of each other -- close enough to be the same idea, not so loose that
# unrelated parameter choices get flagged.
RISK_PARAMETER_SIMILARITY_PCT = float(os.getenv("RISK_PARAMETER_SIMILARITY_PCT", "15.0"))


# --- AI System Administrator (Milestone 8) --------------------------------
# Requirement: "Build BATI's autonomous operating system... It never
# replaces their [the other agents'] logic. It coordinates, validates,
# monitors and recovers them." Every threshold below is a WARNING/ALERT
# threshold, never an auto-remediation trigger for anything that could
# lose data -- see agents/sys_admin/self_healing.py's own docstring for
# the safe-vs-propose-only distinction this whole package holds to.

# Agent Orchestrator: no heartbeat within this window -> "stale" (agent
# hasn't run recently; could be benign -- nothing scheduled -- or could
# mean it stopped working; agents.sys_admin.orchestrator reports the
# fact, never assumes which).
SYS_ADMIN_HEARTBEAT_STALE_MINUTES = int(os.getenv("SYS_ADMIN_HEARTBEAT_STALE_MINUTES", "1440"))  # 24h

# Infrastructure Monitor thresholds (stdlib-only measurements -- psutil
# is not a dependency of this repo; see infra_monitor.py's own docstring
# for exactly what is and isn't measured without it).
SYS_ADMIN_DISK_WARN_PCT = float(os.getenv("SYS_ADMIN_DISK_WARN_PCT", "80.0"))
SYS_ADMIN_DISK_CRITICAL_PCT = float(os.getenv("SYS_ADMIN_DISK_CRITICAL_PCT", "95.0"))
SYS_ADMIN_MEMORY_WARN_PCT = float(os.getenv("SYS_ADMIN_MEMORY_WARN_PCT", "80.0"))
SYS_ADMIN_MEMORY_CRITICAL_PCT = float(os.getenv("SYS_ADMIN_MEMORY_CRITICAL_PCT", "95.0"))
# Load average (1-minute) as a multiple of CPU core count -- >1.0x means
# more runnable processes than cores, a standard *nix load heuristic.
SYS_ADMIN_CPU_LOAD_WARN_MULTIPLIER = float(os.getenv("SYS_ADMIN_CPU_LOAD_WARN_MULTIPLIER", "1.5"))
SYS_ADMIN_DB_LATENCY_WARN_MS = float(os.getenv("SYS_ADMIN_DB_LATENCY_WARN_MS", "250.0"))
# "Queue length" in this codebase means the real approval backlog --
# agent_audit_log rows with outcome='pending_approval' -- not a message
# broker queue (this repo has none, by design; see
# AUTONOMOUS_AGENTS_ARCHITECTURE.md principle 3).
SYS_ADMIN_QUEUE_LENGTH_WARN = int(os.getenv("SYS_ADMIN_QUEUE_LENGTH_WARN", "20"))

# Backup & Recovery.
SYS_ADMIN_BACKUP_DIR = os.getenv("SYS_ADMIN_BACKUP_DIR", "backups")
SYS_ADMIN_BACKUP_RETENTION_COUNT = int(os.getenv("SYS_ADMIN_BACKUP_RETENTION_COUNT", "7"))

# Security: the git ref "unexpected code modification detection" diffs
# the current working tree against. Defaults to HEAD (the last commit --
# i.e. "what changed since the last commit that isn't already explained
# by an in-flight, known change"), not "master", so this also works
# correctly from inside an agent's own isolated worktree.
SYS_ADMIN_KNOWN_GOOD_REF = os.getenv("SYS_ADMIN_KNOWN_GOOD_REF", "HEAD")

# Autonomous Maintenance.
SYS_ADMIN_DUPLICATE_BLOCK_MIN_LINES = int(os.getenv("SYS_ADMIN_DUPLICATE_BLOCK_MIN_LINES", "6"))
SYS_ADMIN_SLOW_TEST_WARN_SECONDS = float(os.getenv("SYS_ADMIN_SLOW_TEST_WARN_SECONDS", "2.0"))
# tracemalloc-based leak probe: growth beyond this, after
# SYS_ADMIN_MEMORY_LEAK_ITERATIONS repetitions of the probed operation,
# is flagged -- a bounded, real (not simulated) measurement, not a claim
# of having run for hours; see maintenance.py's own docstring.
SYS_ADMIN_MEMORY_LEAK_GROWTH_KB = float(os.getenv("SYS_ADMIN_MEMORY_LEAK_GROWTH_KB", "512.0"))
SYS_ADMIN_MEMORY_LEAK_ITERATIONS = int(os.getenv("SYS_ADMIN_MEMORY_LEAK_ITERATIONS", "50"))
SYS_ADMIN_DB_FRAGMENTATION_WARN_PCT = float(os.getenv("SYS_ADMIN_DB_FRAGMENTATION_WARN_PCT", "20.0"))


# --- AI Autonomous Runtime & Orchestration Engine (Milestone 9) -------------
# The runtime layer that actually invokes the six agents above on a
# schedule -- everything below is consumed by agents/runtime/.

RUNTIME_DB_PATH = os.getenv("RUNTIME_DB_PATH", "oi_history.db")

# Scheduler cadence, per agent, in seconds -- how often each agent's
# run_cycle()-equivalent is invoked when the scheduler is running
# continuously. Independently overridable so e.g. quant_researcher (a
# real backtest sweep) can run far less often than sys_admin (a cheap
# health check).
RUNTIME_CADENCE_SECONDS = {
    "dev_agent": int(os.getenv("RUNTIME_CADENCE_DEV_AGENT_SECONDS", "3600")),
    "memory": int(os.getenv("RUNTIME_CADENCE_MEMORY_SECONDS", "1800")),
    "quant_researcher": int(os.getenv("RUNTIME_CADENCE_QUANT_RESEARCHER_SECONDS", "3600")),
    "risk_manager": int(os.getenv("RUNTIME_CADENCE_RISK_MANAGER_SECONDS", "300")),
    "trading_supervisor": int(os.getenv("RUNTIME_CADENCE_TRADING_SUPERVISOR_SECONDS", "300")),
    "sys_admin": int(os.getenv("RUNTIME_CADENCE_SYS_ADMIN_SECONDS", "600")),
    # 180s matches the native 3-minute candle/cycle granularity this
    # engine actually reads (see multi_timeframe.py's own NATIVE_TIMEFRAME
    # finding) -- running more often than the underlying data changes
    # would just re-evaluate the same stored cycle repeatedly.
    "trading_intelligence": int(os.getenv("RUNTIME_CADENCE_TRADING_INTELLIGENCE_SECONDS", "180")),
}

# Which symbols agents.quant_researcher.research_engine.run_research_cycle()
# sweeps on its own scheduled cadence, and how many trailing days of
# already-archived candle/cycle history each cycle evaluates -- the same
# "trailing window from the latest available data" shape
# scripts/hardening/market_replay.py already uses for real, not a new
# pattern invented for the runtime layer.
RUNTIME_RESEARCH_SYMBOLS = tuple(
    s.strip() for s in os.getenv("RUNTIME_RESEARCH_SYMBOLS", "NIFTY,BANKNIFTY").split(",") if s.strip()
)
RUNTIME_RESEARCH_LOOKBACK_DAYS = int(os.getenv("RUNTIME_RESEARCH_LOOKBACK_DAYS", "30"))

# Which symbols agents.trading_supervisor.supervisor_agent.TradingSupervisor
# watches on its own scheduled cadence (its own DEFAULT_WATCHED_SYMBOLS
# constant already exists in that module -- this override exists only for
# the runtime layer's own wiring, matching every other RUNTIME_* pattern).
RUNTIME_SUPERVISOR_SYMBOLS = tuple(
    s.strip() for s in os.getenv("RUNTIME_SUPERVISOR_SYMBOLS", "").split(",") if s.strip()
) or None  # None -> TradingSupervisor's own default

# Task queue: bounded retries with exponential-ish backoff, and a hard
# timeout so one stuck task can never block the queue forever.
RUNTIME_TASK_MAX_ATTEMPTS = int(os.getenv("RUNTIME_TASK_MAX_ATTEMPTS", "3"))
RUNTIME_TASK_RETRY_BACKOFF_SECONDS = float(os.getenv("RUNTIME_TASK_RETRY_BACKOFF_SECONDS", "30.0"))
RUNTIME_TASK_TIMEOUT_SECONDS = float(os.getenv("RUNTIME_TASK_TIMEOUT_SECONDS", "120.0"))

# Agent health scoring: a crashed/failing agent's health_score decays;
# this many consecutive failures before self_healing's automatic restart
# gives up and escalates to the System Administrator instead of retrying
# again (see agents/runtime/agent_runtime.py).
RUNTIME_MAX_CONSECUTIVE_FAILURES_BEFORE_ESCALATION = int(
    os.getenv("RUNTIME_MAX_CONSECUTIVE_FAILURES_BEFORE_ESCALATION", "3")
)

# Milestone 15, Phase 4: Self-Healing Runtime Protection -- a THIRD,
# distinct failure-handling layer from the two above. tick()'s own
# per-cycle exception isolation already exists (an exception there is
# caught and never propagates); RUNTIME_MAX_CONSECUTIVE_FAILURES_
# BEFORE_ESCALATION above is a PER-AGENT restart/escalate mechanism.
# This one is SCHEDULER-level: after this many CONSECUTIVE tick()
# failures (see agents/runtime/circuit_breaker.py), the scheduler stops
# attempting new work entirely for RUNTIME_CIRCUIT_RECOVERY_SECONDS,
# then lets exactly one probe tick through before deciding whether to
# resume or reopen.
RUNTIME_CIRCUIT_FAILURE_THRESHOLD = int(os.getenv("RUNTIME_CIRCUIT_FAILURE_THRESHOLD", "5"))
RUNTIME_CIRCUIT_RECOVERY_SECONDS = int(os.getenv("RUNTIME_CIRCUIT_RECOVERY_SECONDS", "300"))

# Milestone 16, Phase 1: Persistent Runtime Event Log
# (agents/ops/event_log.py's own ops_event_log table) -- how long a row
# is kept before purge_old_events() removes it. Only ever applies to
# that dedicated operational-telemetry table, never to
# agents.event_bus's own agent_events (audit-significant governance
# events -- policy changes, approvals, escalations -- are never time-
# purged).
OPS_EVENT_RETENTION_DAYS = int(os.getenv("OPS_EVENT_RETENTION_DAYS", "30"))

# Milestone 16, Phase 3: Watchdog & Stale-Cycle Detection
# (agents/runtime/watchdog.py). A scheduler is "stale" once
# WATCHDOG_STALE_MULTIPLIER * the scheduler's own tick_interval_seconds
# has passed since the last SUCCESSFUL tick -- the one failure mode
# tick()'s own exception isolation and the Phase 4 circuit breaker
# can't catch on their own: a genuine hang/deadlock produces no
# exception for either to react to. Detection only -- this module never
# restarts, kills, or touches any process; it only ever exposes a
# restart_recommended flag for a human or an external supervisor
# (run_forever_vps.sh, restart.sh) to act on.
WATCHDOG_ENABLED = os.getenv("WATCHDOG_ENABLED", "true").strip().lower() in ("1", "true", "yes")
WATCHDOG_STALE_MULTIPLIER = float(os.getenv("WATCHDOG_STALE_MULTIPLIER", "3"))

# Runtime policy -- see agents/runtime/policy_engine.py for the full
# taxonomy and what each one actually gates. Configurable without a code
# change, exactly as required: an env var, or a live override written to
# the runtime_policy table (agents/runtime/runtime_store.py) via
# approve_cli.py / the dashboard, which always wins over this default
# once set.
RUNTIME_DEFAULT_POLICY = os.getenv("RUNTIME_DEFAULT_POLICY", "recommendation_only")

# Milestone 12, Phase 1: Runtime Scheduler Activation. OFF by default --
# RuntimeScheduler.run_forever() was built in Milestone 9 but never
# actually invoked in production (confirmed by survey: no systemd unit,
# crontab, or app.py call ever started it). Once enabled, it opens real
# (paper) trades and writes to the live database unattended for the
# first time in this project's history, so activation must be an
# explicit, reviewed opt-in, never a silent side effect of deploying
# this code.
RUNTIME_SCHEDULER_ENABLED = os.getenv("RUNTIME_SCHEDULER_ENABLED", "false").strip().lower() in ("1", "true", "yes")

# Single-instance advisory file lock (fcntl.flock) -- prevents two OS
# processes (two gunicorn workers, or an old process still shutting down
# during a restart/reload) from both running the scheduler loop at once.
# app.py's own header comment documents gunicorn as the production WSGI
# server, so this is a real, not hypothetical, concern.
RUNTIME_SCHEDULER_LOCK_PATH = os.getenv("RUNTIME_SCHEDULER_LOCK_PATH", "/tmp/oi_dashboard_runtime_scheduler.lock")

# Milestone 12, Phase 2A: gates the NEW HTTP write endpoints
# (/api/runtime/control/...) that let an admin call policy_engine.
# set_policy()/scheduling_control.set_mode() from the web dashboard
# instead of only via runtime_control_cli.py. OFF by default --
# independent of RUNTIME_SCHEDULER_ENABLED, since this flag governs
# whether the ACTION SURFACE exists at all, not whether the scheduler
# itself is running. Read-only status display (/api/runtime/status) is
# never gated by this -- only the routes that mutate state are.
RUNTIME_CONTROL_API_ENABLED = os.getenv("RUNTIME_CONTROL_API_ENABLED", "false").strip().lower() in ("1", "true", "yes")

# Milestone 13, Phase 3: Dashboard Runtime Visibility. Pure observability
# metadata for the runtime status panel -- neither value gates any
# behavior anywhere in the app. APP_VERSION identifies which milestone's
# code is deployed; ENVIRONMENT distinguishes a live VPS from a local
# checkout. Both are honest, operator-set values (not derived/guessed) --
# APP_VERSION is bumped by hand alongside RELEASE_NOTES_*.md, same as
# every other milestone marker in this codebase.
APP_VERSION = os.getenv("APP_VERSION", "milestone-13-phase3")
ENVIRONMENT = os.getenv("APP_ENV", "production")

# Milestone 14, Phase 1: Intelligence Alerting Layer. Thresholds only --
# nothing here gates execution of any kind. Edited by hand, same
# convention as every other threshold constant in this file; no HTTP
# write route exists to change these (see agents/intelligence_alerts/
# module docstring for why that's deliberate).
INTELLIGENCE_ALERT_CONFIDENCE_WINDOW = int(os.getenv("INTELLIGENCE_ALERT_CONFIDENCE_WINDOW", "5"))
INTELLIGENCE_ALERT_CONFIDENCE_STDEV_THRESHOLD = float(os.getenv("INTELLIGENCE_ALERT_CONFIDENCE_STDEV_THRESHOLD", "15"))
INTELLIGENCE_ALERT_OI_WINDOW = int(os.getenv("INTELLIGENCE_ALERT_OI_WINDOW", "5"))
# Optional -- alert emails are only sent if this is set; Telegram alone
# is fully functional without it (TELEGRAM_BOT_TOKEN/CHAT_ID already
# exist in .env for other notifications in this app).
INTELLIGENCE_ALERT_EMAIL_TO = os.getenv("INTELLIGENCE_ALERT_EMAIL_TO", "") or None

# Milestone 14, Phase 2: automatic (background-loop-driven) intelligence
# snapshot logging + alert evaluation, as an explicit, off-by-default
# opt-in -- same posture as RUNTIME_SCHEDULER_ENABLED. When False (the
# default), Phase 1's behavior is unchanged: a snapshot is only ever
# logged, and a rule only ever evaluated, via intelligence_history_cli.py
# / intelligence_alerts_cli.py, manually. When explicitly set true, each
# symbol's live (never dev-mode/stale) per-cycle loop in
# run_symbol_loop() also logs a fresh snapshot and evaluates alert rules
# -- still strictly read-only/non-trading: this can only ever record a
# row and send a Telegram/email NOTICE, never open, close, or modify a
# trade. INTELLIGENCE_ALERTS_AUTO_COOLDOWN_SECONDS prevents the same
# (symbol, rule) pair from re-alerting every single cycle while a
# condition remains true. Superseded as the ACTIVE gate at that one
# call site by INTELLIGENCE_ALERT_DEDUP_COOLDOWN_SECONDS below
# (Milestone 15, Phase 1) -- left defined and still overridable via the
# same POST /api/intelligence/alerts/config route for backward
# compatibility, but no longer consulted in app.py's auto-cycle.
INTELLIGENCE_ALERTS_AUTO_ENABLED = os.getenv("INTELLIGENCE_ALERTS_AUTO_ENABLED", "false").strip().lower() in ("1", "true", "yes")
INTELLIGENCE_ALERTS_AUTO_COOLDOWN_SECONDS = int(os.getenv("INTELLIGENCE_ALERTS_AUTO_COOLDOWN_SECONDS", "900"))

# Milestone 15, Phase 1: Alert Deduplication & Cooldown Protection.
# Replaces the flat (symbol, rule)-keyed check above as the active gate
# in app.py's auto-cycle: a fingerprint of (symbol, bias, confidence
# bucket, rule) identifies "the same market condition" -- see
# agents/intelligence_alerts/cooldown.py and dedup_store.py for the
# full algorithm, including the bypass rules (bias change, rule change,
# or a confidence-bucket INCREASE all allow an immediate resend even
# inside this cooldown window). No strike component: nothing in this
# codebase's intelligence-alert data carries an option strike.
# Overridable at runtime via the same POST /api/intelligence/alerts/
# config route Phase 3 already shipped -- no new write surface.
INTELLIGENCE_ALERT_DEDUP_COOLDOWN_SECONDS = int(os.getenv("INTELLIGENCE_ALERT_DEDUP_COOLDOWN_SECONDS", "300"))

# Trading Intelligence Cycle web trigger (Today Signal Audit follow-up):
# gates a NEW POST route that calls agents.trading_intelligence.api.
# run_scheduled_cycle() -- the exact same, only-ever-manual entrypoint
# trading_intelligence_cli.py's own `run-cycle` command already calls.
# Off by default, same posture as every other opt-in flag in this file
# -- INDEPENDENT of RUNTIME_CONTROL_API_ENABLED (a different subsystem:
# policy_engine pause/resume/mode, not trade-opening) so enabling one
# never silently enables the other. Does NOT touch
# RUNTIME_SCHEDULER_ENABLED or scheduling_control.NEVER_SCHEDULABLE_AGENTS
# -- this route is a THIRD, still-manual-only trigger (a human clicks a
# button and supplies a reason, same as the CLI requires nothing but a
# human typing a command), never a recurring/scheduled one.
TI_RUN_CYCLE_API_ENABLED = os.getenv("TI_RUN_CYCLE_API_ENABLED", "false").strip().lower() in ("1", "true", "yes")

# Milestone 14, Phase 3: the DEFAULT low-liquidity suppression symbol
# list for the oi_non_responsive alert rule -- moved here from
# agents/intelligence_alerts/rules.py's own hardcoded frozenset so it
# lives alongside every other alert threshold this file already owns,
# now that agents/intelligence_alerts/threshold_store.py makes it
# operator-overridable via POST /api/intelligence/alerts/config. This
# constant is still the value used whenever no override has been set --
# same "edited by hand is still the honest baseline" contract every
# other threshold here has.
INTELLIGENCE_ALERT_LOW_LIQUIDITY_SUPPRESSION_SYMBOLS = tuple(
    s.strip() for s in os.getenv(
        "INTELLIGENCE_ALERT_LOW_LIQUIDITY_SUPPRESSION_SYMBOLS", "GOLD,SILVER,CRUDEOIL,NATURALGAS"
    ).split(",") if s.strip()
)

# Milestone 14, Phase 3: gates a NEW POST route
# (/api/intelligence/alerts/config) that lets an admin override the
# alert thresholds above at runtime instead of editing this file by
# hand. Off by default, same posture as every other opt-in flag here.
# The READ side (GET /api/intelligence/alerts/rules, showing the
# CURRENT effective values) is never gated by this -- only the route
# that mutates state is, matching RUNTIME_CONTROL_API_ENABLED's own
# established precedent for that exact split. Does NOT make
# INTELLIGENCE_ALERTS_AUTO_ENABLED or TI_RUN_CYCLE_API_ENABLED
# themselves configurable this way -- those are "should this run at
# all" feature flags, a fundamentally different, higher-stakes kind of
# toggle that stays .env/restart-gated on purpose; this route only ever
# touches tuning knobs for logic that's already running.
INTELLIGENCE_ALERT_CONFIG_API_ENABLED = os.getenv("INTELLIGENCE_ALERT_CONFIG_API_ENABLED", "false").strip().lower() in ("1", "true", "yes")

# Milestone 15, Phase 0: Bias Flip Stabilization -- alert-layer only.
# The underlying `bias` value itself (oi_engine.py's detect_bias(),
# surfaced unchanged via intelligence_orchestrator.py) is NOT touched by
# this pass -- these two constants only control WHEN
# agents/intelligence_alerts/rules.py's check_bias_flip() fires, not
# what bias is computed as. A bias value now has to hold for
# INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS consecutive logged
# snapshots before it's treated as a real, confirmed flip (set this to
# 1 to reproduce the exact pre-Phase-0 behavior: alert on every single-
# snapshot change vs. the immediately prior one). Once confirmed, the
# rule then applies its own INTELLIGENCE_ALERT_BIAS_FLIP_COOLDOWN_SECONDS
# before it will fire again for the same symbol -- independent of, and
# typically tighter than, the generic auto_cooldown_seconds Phase 2's
# auto-cycle already applies at the delivery layer in app.py; the two
# don't conflict, they just mean the auto-cycle path is bounded by
# whichever of the two is larger, while a direct caller (the CLI, a
# test) is bounded by this one alone. Both are overridable at runtime
# via the same POST /api/intelligence/alerts/config route Phase 3
# already shipped -- no new write surface added here.
INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS = int(os.getenv("INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", "2"))
INTELLIGENCE_ALERT_BIAS_FLIP_COOLDOWN_SECONDS = int(os.getenv("INTELLIGENCE_ALERT_BIAS_FLIP_COOLDOWN_SECONDS", "300"))

# Milestone 15, Phase 2: Alert Rate Limiting & Retry Protection. Caps
# raw alert VOLUME (distinct conditions, not repeats of the same one --
# that's dedup_store.py's job) during a genuinely volatile session where
# many different symbols/rules could legitimately trigger in quick
# succession. Both are rolling 1-hour windows (a send stops counting
# exactly 1 hour after it happened), not calendar-hour buckets.
INTELLIGENCE_ALERT_MAX_PER_SYMBOL_PER_HOUR = int(os.getenv("INTELLIGENCE_ALERT_MAX_PER_SYMBOL_PER_HOUR", "20"))
INTELLIGENCE_ALERT_MAX_TOTAL_PER_HOUR = int(os.getenv("INTELLIGENCE_ALERT_MAX_TOTAL_PER_HOUR", "100"))

# Delivery retry backoff schedule (seconds) -- index 0 is the delay
# before the 2nd attempt, index 1 before the 3rd, etc. Fixed schedule
# rather than an env var (a list isn't a natural single env value, and
# this exact progression is what the Phase 2 spec asked for); the
# attempt CAP is still configurable.
INTELLIGENCE_ALERT_RETRY_BACKOFF_SECONDS = (30, 60, 120, 300)
INTELLIGENCE_ALERT_RETRY_MAX_ATTEMPTS = int(os.getenv("INTELLIGENCE_ALERT_RETRY_MAX_ATTEMPTS", "4"))

# --- BATI Trading Intelligence Platform (Milestone 10) ----------------------
# "This milestone focuses ONLY on trading intelligence, market analysis and
# paper trading" -- config for agents/trading_intelligence/.

TI_WATCHED_SYMBOLS = tuple(
    s.strip() for s in os.getenv(
        "TI_WATCHED_SYMBOLS",
        "NIFTY,BANKNIFTY,SENSEX,NATURALGAS,NATGASMINI,CRUDEOIL,CRUDEOILM,GOLD,GOLDM,SILVER,SILVERM",
    ).split(",") if s.strip()
)
TI_DEFAULT_CAPITAL = float(os.getenv("TI_DEFAULT_CAPITAL", "500000"))
TI_DEFAULT_RISK_PCT = float(os.getenv("TI_DEFAULT_RISK_PCT", "1.0"))

# Milestone 19: minimum Recommendation.confidence for run_scheduled_cycle()
# to post to the Telegram signals channel via telegram_notifier.py --
# every actionable BUY still opens its paper trade regardless of this
# gate (unchanged); this only controls what additionally gets broadcast.
TI_TELEGRAM_MIN_CONFIDENCE = int(os.getenv("TI_TELEGRAM_MIN_CONFIDENCE", "75"))

# Milestone 20, Phase 2: structure-alert-only wiring
# (agents/trading_intelligence/structure_alerts.py) into the live
# scheduler cycle. Off by default -- deploying that module changes
# NOTHING about the live cycle until this is explicitly set true.
# Structure alerts (role flips, breakout/breakdown watch, reversal
# risk) never open a paper trade and never touch ai_trading_engine's
# own BUY CE/PE decision -- this flag only controls whether that
# separate, read-only-on-the-signal-engine evaluation step runs at all.
TI_ENABLE_STRUCTURE_ALERTS = os.getenv("TI_ENABLE_STRUCTURE_ALERTS", "false").strip().lower() in ("1", "true", "yes")

# Milestone 20, Phase 7: the bounded/audited adaptive structure-tuning
# loop (agents/trading_intelligence/structure_tuning.py). Off by
# default -- same "deploying this file changes nothing about the live
# cycle until this is explicitly set true" convention
# TI_ENABLE_STRUCTURE_ALERTS above already established, and for the
# same reason: this gate lives at the agent_runtime.py CALL SITE (not
# inside structure_tuning.evaluate_and_maybe_tune() itself, which the
# CLI's manual `run`/`run --force` commands call directly and must
# always be able to trigger regardless of this flag), so it's what
# keeps a real evaluation pass (a full grid across every
# TI_WATCHED_SYMBOLS symbol, ~1-2 minutes of real backtest work) off
# the hot path of every unrelated test that happens to exercise the TI
# cycle. The live .env explicitly sets this true (same as
# TI_ENABLE_STRUCTURE_ALERTS's own live deployment) -- the loop's own
# internal safety gates (hard parameter bounds, minimum sample size,
# minimum improvement margin, per-parameter cooldown) are unaffected by
# this flag either way; it only controls whether the loop runs
# automatically at all, never whether it's safe once running.
TI_ENABLE_STRUCTURE_TUNING = os.getenv("TI_ENABLE_STRUCTURE_TUNING", "false").strip().lower() in ("1", "true", "yes")

# Milestone 21, Phase 1: the Virtual Trailing Engine
# (agents/trading_intelligence/virtual_trailing.py) -- a paper-trade /
# advisory-only shadow layer that tracks a dynamic trailing SL/target
# alongside each real OPEN paper trade, never touching
# ti_store.close_trade() or any broker path. Off by default -- same
# "deploying this file changes nothing about the live cycle until this
# is explicitly set true" convention every TI_ENABLE_* flag above
# already established.
TI_ENABLE_VIRTUAL_TRAILING = os.getenv("TI_ENABLE_VIRTUAL_TRAILING", "false").strip().lower() in ("1", "true", "yes")

# --- Self-modification guard -------------------------------------------------
# Requirement: "Do not implement any self-modifying production code."
# Hard-coded, not configurable -- there is deliberately no env var here.
# agents/dev_agent/detector.py (Milestone 3) refuses any diff touching a
# path under this prefix before a worktree is even created.
SELF_MODIFICATION_GUARD_PREFIX = "agents/"

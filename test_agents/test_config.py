"""
test_agents/test_config.py -- regression tests for agents/config.py,
particularly priority_for_path() (the "Priority 1/2/3" detection-scope
requirement) and the self-modification guard constant.
"""
from agents import config


class TestPriorityForPath:
    def test_priority_1_trading_core_files(self):
        assert config.priority_for_path("exit_engine_v4.py") == 1
        assert config.priority_for_path("backtest.py") == 1
        assert config.priority_for_path("position_sizing.py") == 1
        assert config.priority_for_path("sr_engine_v3.py") == 1

    def test_priority_2_dashboard_ui(self):
        assert config.priority_for_path("templates/backtest.html") == 2
        assert config.priority_for_path("static/manifest.json") == 2
        assert config.priority_for_path("app.py") == 2

    def test_priority_3_docs(self):
        assert config.priority_for_path("README.md") == 3
        assert config.priority_for_path("AUTONOMOUS_AGENTS_ARCHITECTURE.md") == 3

    def test_unmatched_path_falls_back_to_default_priority(self):
        assert config.priority_for_path("some_random_utility_script.py") == config.DEFAULT_PRIORITY
        assert config.DEFAULT_PRIORITY == 3

    def test_agents_own_directory_is_not_priority_1(self):
        # agents/** must never be treated as trading-core-urgent -- it's
        # visible to the detector (falls through to DEFAULT_PRIORITY) but
        # never elevated, since the detector refuses to touch it regardless.
        assert config.priority_for_path("agents/dev_agent/runner.py") == config.DEFAULT_PRIORITY

    def test_specific_file_not_shadowed_by_a_shorter_directory_prefix(self):
        # "app.py" (priority 2) vs a hypothetical shorter unrelated prefix --
        # longest-match-wins ensures the most specific entry decides.
        assert config.priority_for_path("app.py") == 2


class TestSelfModificationGuard:
    def test_guard_prefix_targets_the_agents_directory(self):
        assert config.SELF_MODIFICATION_GUARD_PREFIX == "agents/"

    def test_guard_prefix_is_not_env_overridable(self):
        # Requirement: "Do not implement any self-modifying production
        # code" -- this must be a hard-coded constant, not a setting a
        # misconfigured environment could loosen. config.py itself is the
        # only source (no os.getenv call feeds this specific constant).
        import inspect

        source = inspect.getsource(config)
        guard_line = next(l for l in source.splitlines() if l.startswith("SELF_MODIFICATION_GUARD_PREFIX"))
        assert "getenv" not in guard_line


class TestRegressionThresholds:
    def test_all_thresholds_default_to_zero_tolerance(self):
        # Requirement: "Reject any change that decreases profitability,
        # increases drawdown, reduces stability" -- zero tolerance by
        # default, not a "flag for review" tolerance band.
        assert config.MAX_NET_PNL_REGRESSION_PCT == 0.0
        assert config.MAX_PROFIT_FACTOR_REGRESSION_PCT == 0.0
        assert config.MAX_EXPECTANCY_REGRESSION_PCT == 0.0
        assert config.MAX_DRAWDOWN_INCREASE_PCT == 0.0


class TestBenchmarkBaseline:
    def test_baseline_matches_this_sessions_established_comparisons(self):
        assert config.BENCHMARK_BASELINE["symbol"] == "NIFTY"
        assert config.BENCHMARK_BASELINE["date_from"] == "2026-05-01"
        assert config.BENCHMARK_BASELINE["date_to"] == "2026-08-04"

    def test_available_symbols_covers_the_requested_expansion_set(self):
        for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
            assert sym in config.BENCHMARK_AVAILABLE_SYMBOLS


class TestTiWatchedSymbols:
    # FINNIFTY and MIDCPNIFTY have full, correctly-populated option-chain
    # data in production (verified against the live DB: 9/9 strikes, real
    # contract identity, no strike-step bug) but were never added to this
    # list when it was extended for MCX commodities -- an unrevisited scope
    # gap, not a deliberate exclusion. This locks in their inclusion.
    def test_nse_index_symbols_with_option_chains_are_watched(self):
        for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
            assert sym in config.TI_WATCHED_SYMBOLS

    def test_mcx_commodity_symbols_are_watched(self):
        for sym in ("NATURALGAS", "NATGASMINI", "CRUDEOIL", "CRUDEOILM",
                    "GOLD", "GOLDM", "SILVER", "SILVERM"):
            assert sym in config.TI_WATCHED_SYMBOLS

    def test_india_vix_is_not_watched(self):
        # INDIA VIX is a spot-only index with no option chain
        # (app.py SYMBOLS["INDIA VIX"]["type"] == "index_spot") -- there is
        # no contract for the TI engine to ever recommend, so it must never
        # appear here.
        assert "INDIA VIX" not in config.TI_WATCHED_SYMBOLS

    def test_bankex_is_not_watched(self):
        # BANKEX is not configured anywhere in app.py's SYMBOLS dict.
        assert "BANKEX" not in config.TI_WATCHED_SYMBOLS


class TestTriggers:
    def test_no_timer_or_interval_style_trigger_present(self):
        triggers_text = " ".join(config.DEV_AGENT_TRIGGERS).lower()
        for banned in ("timer", "interval", "cron", "schedule"):
            assert banned not in triggers_text

    def test_all_required_triggers_present(self):
        required = {"commit", "pull_request", "manual", "test_failure",
                    "backtest_failure", "runtime_exception", "performance_regression"}
        assert required == set(config.DEV_AGENT_TRIGGERS)

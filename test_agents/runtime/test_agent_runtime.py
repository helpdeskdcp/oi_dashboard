from agents.runtime import agent_runtime as ar


class TestRunAgentCycle:
    def test_rejects_unknown_agent(self, agent_db, memory_store):
        import pytest
        with pytest.raises(ValueError):
            ar.run_agent_cycle("not_a_real_agent", memory_store=memory_store)

    def test_memory_cycle_succeeds_when_store_is_reachable(self, agent_db, memory_store):
        result = ar.run_agent_cycle("memory", memory_store=memory_store)
        assert result["success"] is True
        assert result["status"]["health_score"] == 100

    def test_sys_admin_cycle_runs_and_records_execution(self, agent_db, memory_store, tmp_path):
        result = ar.run_agent_cycle("sys_admin", memory_store=memory_store, repo_dir=".")
        assert result["success"] is True
        assert result["status"]["last_execution_ts"] is not None
        assert result["status"]["last_execution_duration_ms"] >= 0

    def test_trading_supervisor_cycle_runs_and_updates_heartbeat(self, agent_db, memory_store):
        result = ar.run_agent_cycle("trading_supervisor", memory_store=memory_store)
        assert result["success"] is True
        assert result["status"]["last_heartbeat_ts"] is not None

    def test_dev_agent_cycle_is_an_honest_noop_on_empty_queue(self, agent_db, memory_store):
        result = ar.run_agent_cycle("dev_agent", memory_store=memory_store)
        assert result["success"] is True
        assert "no dev_agent_trigger tasks queued" in result["findings"][0]["summary"]

    def test_dev_agent_cycle_drains_a_queued_trigger(self, agent_db, memory_store, toy_repo, monkeypatch):
        """Proves the Task Queue module actually triggers the AI
        Developer -- not just documented intent. Monkeypatches
        agents.dev_agent.pipeline.run_proposal() itself (never the LLM
        layer directly, and never a real network call) -- dev_agent's
        OWN detect/patch/gate logic is already thoroughly covered by
        test_agents/dev_agent/; this test is only about the QUEUE->AGENT
        wiring agent_runtime.py's _dev_agent_cycle adds."""
        from agents.runtime import task_queue

        calls = []

        def fake_run_proposal(repo_dir, trigger, target_files, *, base_ref="main", memory_store=None):
            calls.append({"repo_dir": repo_dir, "trigger": trigger, "target_files": target_files, "base_ref": base_ref})

        import agents.dev_agent.pipeline as pipeline_module
        monkeypatch.setattr(pipeline_module, "run_proposal", fake_run_proposal)

        task_queue.enqueue(
            priority="high", task_type="dev_agent_trigger",
            payload={"trigger": "manual_test", "target_files": ["README.md"], "base_ref": "main"},
        )
        result = ar.run_agent_cycle("dev_agent", memory_store=memory_store, repo_dir=str(toy_repo))
        assert calls == [{"repo_dir": str(toy_repo), "trigger": "manual_test",
                           "target_files": ["README.md"], "base_ref": "main"}]
        assert result["success"] is True
        assert "processed queued trigger" in result["findings"][0]["summary"]

    def test_risk_manager_cycle_fails_honestly_without_a_live_schema(self, agent_db, memory_store, risk_data_access_db):
        result = ar.run_agent_cycle("risk_manager", memory_store=memory_store)
        assert result["success"] is False
        assert "no such table" in result["error"]

    def test_risk_manager_cycle_succeeds_against_a_real_schema(self, agent_db, memory_store, paper_db):
        from test_agents.risk_manager.conftest import insert_user
        insert_user(paper_db, 1)
        result = ar.run_agent_cycle("risk_manager", memory_store=memory_store)
        assert result["success"] is True

    def test_quant_researcher_cycle_runs_against_real_candle_archives(self, agent_db, memory_store):
        result = ar.run_agent_cycle("quant_researcher", memory_store=memory_store, repo_dir=".")
        assert result["success"] is True
        assert len(result["findings"]) >= 1

    def test_trading_intelligence_cycle_runs_with_no_data_and_reports_honestly(self, agent_db, memory_store, ti_db):
        """No cycle logged for any watched symbol -- every symbol should
        come back "unavailable" honestly, never raise."""
        result = ar.run_agent_cycle("trading_intelligence", memory_store=memory_store)
        assert result["success"] is True
        assert len(result["findings"]) == 3  # NIFTY, BANKNIFTY, SENSEX (config.TI_WATCHED_SYMBOLS)
        assert all("unavailable" in f["summary"] for f in result["findings"])

    def test_trading_intelligence_cycle_opens_a_paper_trade_from_a_real_buy_signal(self, agent_db, memory_store, ti_db):
        import sqlite3

        from agents.trading_intelligence import ti_store as ts
        from test_agents.trading_intelligence.conftest import insert_realistic_chain

        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()

        result = ar.run_agent_cycle("trading_intelligence", memory_store=memory_store)
        assert result["success"] is True
        nifty_finding = next(f for f in result["findings"] if f["summary"].startswith("NIFTY"))
        assert "paper trade" in nifty_finding["summary"]
        assert len(ts.list_open_trades(symbol="NIFTY")) == 1

    def test_disabled_agent_is_skipped_not_run(self, agent_db, memory_store):
        from agents.sys_admin import orchestrator
        orchestrator.disable_agent("risk_manager", reason="maintenance")
        result = ar.run_agent_cycle("risk_manager", memory_store=memory_store)
        assert result["skipped"] == "disabled"
        assert result["success"] is None


class TestShadowModeCycle:
    """Milestone 12, Phase 3: shadow_mode is registered for status/
    health tracking only -- its cycle function must be a read-only
    heartbeat, never a trigger for real observation/evaluation."""

    def test_cycle_succeeds_and_is_never_disabled_by_orchestrator(self, agent_db, memory_store):
        # shadow_mode is NOT in orchestrator.AGENT_NAMES (same treatment
        # as memory/sys_admin/trading_intelligence) -- always enabled,
        # never skippable via orchestrator.disable_agent().
        result = ar.run_agent_cycle("shadow_mode", memory_store=memory_store)
        assert result["success"] is True
        assert "skipped" not in result

    def test_cycle_reports_zero_observations_honestly_when_none_exist(self, agent_db, memory_store):
        result = ar.run_agent_cycle("shadow_mode", memory_store=memory_store)
        assert "no observations recorded yet" in result["findings"][0]["summary"]

    def test_cycle_reports_real_counts_when_observations_exist(self, agent_db, memory_store):
        from agents.shadow_mode import store as shadow_store
        obs_id = shadow_store.record_observation(ts="2026-08-09T10:00:00", symbol="NIFTY", timeframe="3m")
        shadow_store.record_prediction(
            observation_id=obs_id, ts="2026-08-09T10:00:00", symbol="NIFTY", timeframe="3m", signal_type="BUY CE",
        )
        result = ar.run_agent_cycle("shadow_mode", memory_store=memory_store)
        assert result["success"] is True
        summary = result["findings"][0]["summary"]
        assert "1 observation" in summary
        assert "1 prediction" in summary

    def test_cycle_never_writes_to_shadow_observations_or_predictions(self, agent_db, memory_store):
        """The core safety property: a runtime cycle call must not be a
        second execution trigger. Row counts before/after must be
        identical -- this cycle only READS agents.shadow_mode.api.
        get_status()."""
        from agents.shadow_mode import store as shadow_store
        before_obs, before_pred = shadow_store.count_observations(), shadow_store.count_predictions()
        ar.run_agent_cycle("shadow_mode", memory_store=memory_store)
        ar.run_agent_cycle("shadow_mode", memory_store=memory_store)
        after_obs, after_pred = shadow_store.count_observations(), shadow_store.count_predictions()
        assert before_obs == after_obs
        assert before_pred == after_pred

    def test_cycle_updates_execution_bookkeeping(self, agent_db, memory_store):
        result = ar.run_agent_cycle("shadow_mode", memory_store=memory_store)
        assert result["status"]["last_execution_ts"] is not None
        assert result["status"]["health_score"] == 100

    def test_shadow_mode_is_permanently_unschedulable(self, agent_db, memory_store):
        from agents.runtime import scheduling_control as sc
        assert sc.is_schedulable("shadow_mode") is False
        assert "shadow_mode" in sc.NEVER_SCHEDULABLE_AGENTS


class TestFailureHandlingAndEscalation:
    def test_a_failure_increments_failure_counter_and_lowers_health(self, agent_db, memory_store, risk_data_access_db):
        r1 = ar.run_agent_cycle("risk_manager", memory_store=memory_store)
        assert r1["status"]["failure_counter"] == 1
        assert r1["status"]["health_score"] == 80

    def test_a_success_after_failures_resets_the_counter(self, agent_db, memory_store, risk_data_access_db):
        import sqlite3
        ar.run_agent_cycle("risk_manager", memory_store=memory_store)  # fails: no schema yet

        conn = sqlite3.connect(risk_data_access_db)
        conn.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, wallet_balance REAL DEFAULT 50000);
            CREATE TABLE paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, symbol TEXT, strike REAL,
                direction TEXT, entry_price REAL, target_price REAL, sl_price REAL, qty INTEGER DEFAULT 1,
                entry_time TEXT, entry_ts REAL, exit_price REAL, exit_time TEXT, exit_reason TEXT,
                points REAL, status TEXT DEFAULT 'OPEN'
            );
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, strike REAL, direction TEXT,
                entry_price REAL, target_price REAL, sl_price REAL, entry_time TEXT, entry_ts REAL,
                exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL, status TEXT DEFAULT 'OPEN'
            );
            CREATE TABLE scalp_paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, strike REAL, direction TEXT,
                entry_price REAL, target_price REAL, sl_price REAL, entry_time TEXT, entry_ts REAL,
                exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL, status TEXT DEFAULT 'OPEN'
            );
            CREATE TABLE v3_paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, strike REAL, direction TEXT,
                entry_price REAL, target_price REAL, sl_price REAL, entry_time TEXT, entry_ts REAL,
                exit_price REAL, exit_time TEXT, exit_reason TEXT, points REAL, status TEXT DEFAULT 'OPEN'
            );
            CREATE TABLE cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, ts TEXT);
            CREATE TABLE strikes (
                cycle_id INTEGER, strike REAL,
                ce_delta REAL, ce_gamma REAL, ce_theta REAL, ce_vega REAL, ce_iv REAL,
                pe_delta REAL, pe_gamma REAL, pe_theta REAL, pe_vega REAL, pe_iv REAL
            );
        """)
        conn.commit()
        conn.close()

        r2 = ar.run_agent_cycle("risk_manager", memory_store=memory_store)
        assert r2["success"] is True
        assert r2["status"]["failure_counter"] == 0
        assert r2["status"]["health_score"] == 90

    def test_a_failed_cycle_is_immediately_auto_healed_by_orchestrator(self, agent_db, memory_store, risk_data_access_db):
        """See agent_runtime.py's own docstring: a failure records a
        crash AND immediately self-heals it (the one safe, automatic
        recovery this framework performs) -- the crashed flag ends up
        cleared even though the failure itself is fully recorded via
        failure_counter/health_score above."""
        from agents.sys_admin import sysadmin_store
        ar.run_agent_cycle("risk_manager", memory_store=memory_store)
        assert sysadmin_store.get_agent_status("risk_manager")["crashed"] == 0

    def test_escalation_fires_after_configured_consecutive_failures(self, agent_db, memory_store, risk_data_access_db):
        from agents import config, event_bus
        for _ in range(config.RUNTIME_MAX_CONSECUTIVE_FAILURES_BEFORE_ESCALATION):
            ar.run_agent_cycle("risk_manager", memory_store=memory_store)
        events = event_bus.events_since("2020-01-01", event_type="agent_escalated")
        assert len(events) == 1
        assert events[0]["payload_json"]["agent"] == "risk_manager"


class TestHealthSnapshot:
    def test_returns_all_six_runtime_agents(self, agent_db):
        snapshot = ar.health_snapshot()
        assert set(snapshot.keys()) == set(ar.RUNTIME_AGENT_NAMES)

    def test_never_run_agents_report_none(self, agent_db):
        snapshot = ar.health_snapshot()
        assert snapshot["dev_agent"] is None

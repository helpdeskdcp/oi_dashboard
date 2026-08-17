from agents.trading_intelligence import trade_guardian_store


def _plan(**overrides):
    plan = {
        "position_id": "NATURALGAS_250.0_CE_2026-08-17T09:00:00",
        "symbol": "NATURALGAS", "expiry": "2026-08-26", "strike": 250.0, "direction": "CE",
        "entry_price": 9.20, "quantity": 1, "original_sl": 6.50, "original_t1": 11.0,
        "original_t2": 13.0, "original_t3": 15.0, "entry_timestamp": "2026-08-17T09:00:00",
        "signal_reference": None, "registered_by": "test",
    }
    plan.update(overrides)
    return plan


class TestRegisterPlanImmutability:
    def test_register_then_get_roundtrip(self, ti_db):
        pid = trade_guardian_store.register_plan(_plan())
        stored = trade_guardian_store.get_plan(pid)
        assert stored["symbol"] == "NATURALGAS"
        assert stored["entry_price"] == 9.20
        assert stored["original_sl"] == 6.50
        assert stored["original_t1"] == 11.0

    def test_re_registering_same_position_id_never_overwrites(self, ti_db):
        trade_guardian_store.register_plan(_plan())
        # A second call with DIFFERENT values for the same position_id --
        # must be silently ignored, original values must survive untouched.
        trade_guardian_store.register_plan(_plan(entry_price=999.0, original_sl=1.0, original_t1=2.0))
        stored = trade_guardian_store.get_plan("NATURALGAS_250.0_CE_2026-08-17T09:00:00")
        assert stored["entry_price"] == 9.20
        assert stored["original_sl"] == 6.50
        assert stored["original_t1"] == 11.0

    def test_get_plan_returns_none_for_unregistered_position(self, ti_db):
        assert trade_guardian_store.get_plan("NOT_REGISTERED") is None

    def test_list_plans_returns_all_registered(self, ti_db):
        trade_guardian_store.register_plan(_plan(position_id="A", symbol="NIFTY"))
        trade_guardian_store.register_plan(_plan(position_id="B", symbol="BANKNIFTY"))
        symbols = {p["symbol"] for p in trade_guardian_store.list_plans()}
        assert symbols == {"NIFTY", "BANKNIFTY"}


class TestStateUpsert:
    def test_upsert_then_get_state(self, ti_db):
        trade_guardian_store.upsert_state({
            "position_id": "A", "state": "MONITORING", "smart_sl": 6.50, "smart_target_low": 11.0,
            "smart_target_high": 12.0, "breakout_target": None, "trade_health_score": 62.0,
            "trade_health_tier": "CAUTION", "action": "HOLD WITH CAUTION", "reason": "test",
        })
        state = trade_guardian_store.get_state("A")
        assert state["state"] == "MONITORING"
        assert state["trade_health_tier"] == "CAUTION"

    def test_upsert_overwrites_previous_state_for_same_position(self, ti_db):
        trade_guardian_store.upsert_state({"position_id": "A", "state": "MONITORING", "action": "HOLD"})
        trade_guardian_store.upsert_state({"position_id": "A", "state": "TRAILING", "action": "TRAIL"})
        state = trade_guardian_store.get_state("A")
        assert state["state"] == "TRAILING"
        assert state["action"] == "TRAIL"

    def test_state_survives_a_fresh_connection(self, ti_db):
        # Restart/state-recovery: nothing here is in-memory-only -- a
        # brand-new sqlite3.connect() (simulating a process restart) must
        # see exactly what was last upserted.
        trade_guardian_store.upsert_state({"position_id": "A", "state": "MONITORING", "action": "HOLD"})
        import sqlite3
        conn = sqlite3.connect(ti_db)
        row = conn.execute("SELECT state FROM trade_guardian_state WHERE position_id='A'").fetchone()
        conn.close()
        assert row[0] == "MONITORING"

    def test_list_states_active_only_excludes_terminal_states(self, ti_db):
        trade_guardian_store.upsert_state({"position_id": "A", "state": "MONITORING", "action": "HOLD"})
        trade_guardian_store.upsert_state({"position_id": "B", "state": "EXIT / THESIS INVALIDATED", "action": "EXIT / THESIS INVALIDATED"})
        active = [s["position_id"] for s in trade_guardian_store.list_states(active_only=True)]
        assert active == ["A"]


class TestDecisionLog:
    def test_log_decision_then_recent(self, ti_db):
        trade_guardian_store.log_decision({
            "position_id": "A", "state": "MONITORING", "underlying_ltp": 256.0, "current_premium": 9.55,
            "smart_sl": 6.50, "smart_target_low": 11.0, "smart_target_high": 12.0, "breakout_target": None,
            "trade_health_score": 62.0, "trade_health_tier": "CAUTION", "action": "HOLD WITH CAUTION",
            "reason": "test", "component_scores": {"trend": {"score": 55.0, "reason": "x"}},
            "target_feasibility": {"T1": {"premium": 11.0, "verdict": "SUPPORTED"}}, "data_quality": {},
            "error": None,
        })
        rows = trade_guardian_store.recent_decisions("A", limit=5)
        assert len(rows) == 1
        assert rows[0]["action"] == "HOLD WITH CAUTION"
        assert "trend" in rows[0]["component_scores_json"]

    def test_decision_log_is_append_only(self, ti_db):
        for i in range(3):
            trade_guardian_store.log_decision({"position_id": "A", "state": "MONITORING", "action": "HOLD"})
        rows = trade_guardian_store.recent_decisions("A", limit=10)
        assert len(rows) == 3

    def test_recent_decisions_orders_newest_first(self, ti_db):
        trade_guardian_store.log_decision({"position_id": "A", "action": "HOLD"})
        trade_guardian_store.log_decision({"position_id": "A", "action": "TRAIL"})
        rows = trade_guardian_store.recent_decisions("A", limit=5)
        assert rows[0]["action"] == "TRAIL"
        assert rows[1]["action"] == "HOLD"

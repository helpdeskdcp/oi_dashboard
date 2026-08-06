"""
test_agents/trading_supervisor/test_supervisor_agent.py -- regression
tests for TradingSupervisor(BaseAgent). Real SQLiteMemoryStore (tmp_path)
+ the shared agent_db fixture (audit_log/event_bus/risk_store/
supervision_store all pointed at a throwaway file); market_state/
data_health are monkeypatched so tests never need a real candle/cycle
archive.
"""
from agents import event_bus, registry
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.trading_supervisor import data_health, market_state, supervision_store, supervisor_agent
from agents.trading_supervisor.supervisor_agent import TradingSupervisor


def _quiet_market(monkeypatch):
    monkeypatch.setattr(
        supervisor_agent.data_health, "check_feed_staleness",
        lambda symbol, **k: data_health.DataHealth(
            symbol=symbol, latest_cycle_ts="t", staleness_minutes=1.0, is_stale=False, note="fresh",
        ),
    )
    monkeypatch.setattr(
        supervisor_agent.market_state, "volatility_regime",
        lambda **k: {"level": "normal", "vix": 14.0, "percentile": 50.0},
    )


class TestRegistration:
    def test_registered_under_trading_supervisor(self):
        assert registry.get_agent("trading_supervisor") is TradingSupervisor

    def test_is_a_base_agent(self):
        from agents.base_agent import BaseAgent
        assert issubclass(TradingSupervisor, BaseAgent)


class TestRunCycle:
    def test_clean_state_produces_no_findings(self, agent_db, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        _quiet_market(monkeypatch)
        supervisor = TradingSupervisor(memory_store=store, watched_symbols=("NIFTY",))
        findings = supervisor.run_cycle()
        assert findings == []

    def test_records_a_health_snapshot_per_agent(self, agent_db, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        _quiet_market(monkeypatch)
        supervisor = TradingSupervisor(memory_store=store, watched_symbols=("NIFTY",))
        supervisor.run_cycle()
        snapshots = supervision_store.list_agent_health()
        agents_seen = {s["agent"] for s in snapshots}
        assert agents_seen == {"dev_agent", "quant_researcher", "risk_manager", "memory"}

    def test_high_failure_rate_produces_a_critical_finding(self, agent_db, tmp_path, monkeypatch):
        from agents import audit_log
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        _quiet_market(monkeypatch)
        for _ in range(5):
            audit_log.record(agent="dev_agent", action_type="proposal", description="p",
                              risk_tier="needs_approval", outcome="rejected")
        supervisor = TradingSupervisor(memory_store=store, watched_symbols=("NIFTY",))
        findings = supervisor.run_cycle()
        assert any(f.severity == "critical" and "dev_agent" in f.summary for f in findings)

    def test_critical_finding_is_published_to_event_bus(self, agent_db, tmp_path, monkeypatch):
        from agents import audit_log
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        _quiet_market(monkeypatch)
        for _ in range(5):
            audit_log.record(agent="dev_agent", action_type="proposal", description="p",
                              risk_tier="needs_approval", outcome="rejected")
        supervisor = TradingSupervisor(memory_store=store, watched_symbols=("NIFTY",))
        supervisor.run_cycle()
        events = event_bus.events_since("2000-01-01")
        assert any(e["event_type"] == "supervisor_alert" for e in events)

    def test_stale_feed_produces_a_warning_finding(self, agent_db, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        monkeypatch.setattr(
            supervisor_agent.data_health, "check_feed_staleness",
            lambda symbol, **k: data_health.DataHealth(
                symbol=symbol, latest_cycle_ts="t", staleness_minutes=100.0, is_stale=True, note="stale",
            ),
        )
        monkeypatch.setattr(
            supervisor_agent.market_state, "volatility_regime",
            lambda **k: {"level": "normal", "vix": 14.0, "percentile": 50.0},
        )
        supervisor = TradingSupervisor(memory_store=store, watched_symbols=("NIFTY",))
        findings = supervisor.run_cycle()
        assert any(f.severity == "warning" and "stale" in f.summary for f in findings)

    def test_unreachable_memory_produces_a_critical_finding(self, agent_db, monkeypatch):
        class BrokenStore:
            def search_bug_fixes(self, *a, **k):
                raise RuntimeError("gone")

        _quiet_market(monkeypatch)
        supervisor = TradingSupervisor(memory_store=BrokenStore(), watched_symbols=("NIFTY",))
        findings = supervisor.run_cycle()
        assert any(f.severity == "critical" and "Memory" in f.summary for f in findings)


class TestOnEvent:
    def test_critical_risk_alert_is_escalated(self, agent_db):
        supervisor = TradingSupervisor()
        supervisor.on_event({
            "event_type": "risk_alert", "severity": "critical",
            "payload_json": {"metric": "portfolio_heat", "message": "too hot"},
        })
        events = event_bus.events_since("2000-01-01")
        assert any(e["event_type"] == "supervisor_escalation" for e in events)

    def test_non_critical_risk_alert_is_ignored(self, agent_db):
        supervisor = TradingSupervisor()
        supervisor.on_event({"event_type": "risk_alert", "severity": "warning", "payload_json": {}})
        events = event_bus.events_since("2000-01-01")
        assert not any(e["event_type"] == "supervisor_escalation" for e in events)

    def test_unrelated_event_type_is_ignored(self, agent_db):
        supervisor = TradingSupervisor()
        supervisor.on_event({"event_type": "something_else", "severity": "critical", "payload_json": {}})
        events = event_bus.events_since("2000-01-01")
        assert not any(e["event_type"] == "supervisor_escalation" for e in events)

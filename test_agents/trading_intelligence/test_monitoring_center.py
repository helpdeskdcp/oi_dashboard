"""
test_agents/trading_intelligence/test_monitoring_center.py -- Milestone
21, Phase 2: regression tests for monitoring_center.py, the Autonomous
Trade Control Center's read-only aggregation layer. Mocks
intelligence_orchestrator.build_snapshot() and sysadmin_store.
get_agent_status() throughout -- this file tests the AGGREGATION logic,
not those modules' own internals (see their own test files for that).
"""
import types

import intelligence_orchestrator
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import monitoring_center as mc
from agents.trading_intelligence import virtual_trailing as vt


def _state(*, trade_id, symbol="NIFTY", entry_price=100.0, direction="CE"):
    return vt._init_state(
        {"id": trade_id, "symbol": symbol, "direction": direction, "entry_price": entry_price,
         "sl_price": entry_price - 10, "target_price": entry_price + 20, "strike": 24500},
        now="2026-08-14T09:15:00",
    )


class TestPauseResumeReset:
    def test_pause_and_resume_delegate_to_virtual_trailing(self, ti_db):
        vt.init_db()
        assert vt.is_paused() is False
        mc.pause_monitoring()
        assert vt.is_paused() is True
        mc.resume_monitoring()
        assert vt.is_paused() is False

    def test_reset_trade_delegates_to_virtual_trailing(self, ti_db):
        vt.init_db()
        vt.upsert_state(_state(trade_id=1))
        assert mc.reset_trade(1) is True
        assert vt.get_state(1) is None
        assert mc.reset_trade(1) is False   # already gone


class TestGetControlCenterSnapshot:
    def test_empty_state_reports_honest_zeros(self, ti_db, monkeypatch):
        vt.init_db()
        monkeypatch.setattr(sysadmin_store, "get_agent_status", lambda agent: None)
        snap = mc.get_control_center_snapshot()
        assert snap["active_auto_paper_trades"] == 0
        assert snap["total_locked_profit"] == 0.0
        assert snap["highest_premium_captured"] == 0.0
        assert snap["virtual_trailing_status"] == {}
        assert snap["trades"] == []
        assert snap["ai_bias"] is None
        assert snap["scheduler_health"] is None

    def test_no_symbol_means_no_intel_card(self, ti_db, monkeypatch):
        vt.init_db()
        monkeypatch.setattr(sysadmin_store, "get_agent_status", lambda agent: None)
        called = []
        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", lambda *a, **kw: called.append(1))
        snap = mc.get_control_center_snapshot(symbol=None)
        assert snap["ai_bias"] is None
        assert called == []   # never even called without a symbol

    def test_symbol_populates_the_intel_card(self, ti_db, monkeypatch):
        vt.init_db()
        monkeypatch.setattr(sysadmin_store, "get_agent_status", lambda agent: None)
        fake = types.SimpleNamespace(to_dict=lambda: {"bias": "BULLISH", "confidence": 82, "institutional_score": 77})
        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", lambda symbol, **kw: fake)
        snap = mc.get_control_center_snapshot(symbol="NIFTY")
        assert snap["ai_bias"] == "BULLISH"
        assert snap["ai_confidence"] == 82
        assert snap["institutional_score"] == 77

    def test_a_build_snapshot_failure_never_crashes_the_control_center(self, ti_db, monkeypatch):
        vt.init_db()
        monkeypatch.setattr(sysadmin_store, "get_agent_status", lambda agent: None)

        def _raise(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", _raise)
        snap = mc.get_control_center_snapshot(symbol="NIFTY")
        assert snap["ai_bias"] is None

    def test_a_none_snapshot_is_handled_honestly(self, ti_db, monkeypatch):
        vt.init_db()
        monkeypatch.setattr(sysadmin_store, "get_agent_status", lambda agent: None)
        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", lambda symbol, **kw: None)
        snap = mc.get_control_center_snapshot(symbol="NIFTY")
        assert snap["ai_bias"] is None

    def test_aggregates_locked_profit_and_highest_premium_across_active_trades_only(self, ti_db, monkeypatch):
        vt.init_db()
        monkeypatch.setattr(sysadmin_store, "get_agent_status", lambda agent: None)
        monkeypatch.setattr(vt, "current_premium", lambda row: 999.0)   # no strike history needed for this test

        active1 = vt.evaluate_trade(_state(trade_id=1), 113.0)   # gain=13 -> +12 tier, locked=some > 0
        active2 = vt.evaluate_trade(_state(trade_id=2, entry_price=200.0), 209.0)   # gain=9 -> breakeven, locked=0
        exited = vt.evaluate_trade(_state(trade_id=3, entry_price=50.0), 40.0)   # exits immediately, never "active"
        vt.upsert_state(active1)
        vt.upsert_state(active2)
        vt.upsert_state(exited)

        snap = mc.get_control_center_snapshot()
        assert snap["active_auto_paper_trades"] == 2
        assert snap["total_locked_profit"] == round(active1["locked_profit"] + active2["locked_profit"], 2)
        assert snap["highest_premium_captured"] == max(active1["highest_premium"], active2["highest_premium"],
                                                         exited["highest_premium"])
        assert snap["virtual_trailing_status"].get("CLOSED") == 1
        assert len(snap["trades"]) == 3
        assert all(t["current_premium"] == 999.0 for t in snap["trades"])

    def test_scheduler_health_is_passed_through_as_is(self, ti_db, monkeypatch):
        vt.init_db()
        fake_status = {"agent": "trading_intelligence", "health_score": 100, "currently_running": 0}
        monkeypatch.setattr(sysadmin_store, "get_agent_status", lambda agent: fake_status if agent == "trading_intelligence" else None)
        snap = mc.get_control_center_snapshot()
        assert snap["scheduler_health"] == fake_status

    def test_monitoring_paused_reflects_virtual_trailing_state(self, ti_db, monkeypatch):
        vt.init_db()
        monkeypatch.setattr(sysadmin_store, "get_agent_status", lambda agent: None)
        assert mc.get_control_center_snapshot()["monitoring_paused"] is False
        vt.set_paused(True)
        assert mc.get_control_center_snapshot()["monitoring_paused"] is True

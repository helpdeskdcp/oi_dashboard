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
from agents.trading_intelligence import ti_store
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

        # Milestone 21 Phase 2 audit fix: get_control_center_snapshot() now
        # cross-checks each ACTIVE row's trade_id against ti_store's own
        # still-open trades (see TestOrphanedActiveStateAfterRealTradeCloses)
        # -- these two need a matching real, still-OPEN ti_paper_trades row
        # or they'd be (correctly) tagged orphaned and excluded from the
        # aggregates this test is checking.
        trade_id_1 = ti_store.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                          target_price=120.0, sl_price=90.0, qty=1)
        trade_id_2 = ti_store.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=200.0,
                                          target_price=220.0, sl_price=190.0, qty=1)

        active1 = vt.evaluate_trade(_state(trade_id=trade_id_1), 113.0)   # gain=13 -> +12 tier, locked=some > 0
        active2 = vt.evaluate_trade(_state(trade_id=trade_id_2, entry_price=200.0), 209.0)   # gain=9 -> breakeven, locked=0
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
        assert all(not t["orphaned"] for t in snap["trades"] if t["trade_id"] in (trade_id_1, trade_id_2))

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


class TestOrphanedActiveStateAfterRealTradeCloses:
    """Milestone 21 Phase 2 data-integrity audit finding (production
    evidence: virtual_trailing_state trade_ids 33/53/59/60/61 in the live
    oi_history.db, all state=ACTIVE with their corresponding ti_paper_trades
    row already status=CLOSED -- matching the CRUDEOILM/CRUDEOIL/SILVER
    anomalies reported against the Control Center), NOW FIXED.

    Root cause: once the REAL trade behind a virtual_trailing_state row
    closes (ti_store.close_trade()), virtual_trailing.run_virtual_trailing_
    cycle() stops evaluating that trade_id forever -- by design, see
    test_virtual_trailing.py's own test_state_is_frozen_once_the_real_
    trade_closes, which is correct at THAT layer and is NOT changed by
    this fix. The gap was one layer up: nothing ever reconciled the
    now-frozen row's `state` field against the real trade's now-CLOSED
    status. get_control_center_snapshot() used to blindly trust
    virtual_trailing_state.state == 'ACTIVE' with no cross-check against
    ti_store, so a dead position kept inflating
    active_auto_paper_trades/total_locked_profit and kept appearing in the
    Control Center's trade grid tagged ACTIVE indefinitely.

    Fix (monitoring_center._enrich_trade(), read-time only, per the
    audit's own "fix the source of truth, don't hide rows" instruction):
    each ACTIVE row is now cross-checked against ti_store.list_open_trades()
    and tagged `orphaned=True` when its real trade is no longer open. The
    orphaned row's own stored fields (highest_premium/virtual_sl/
    locked_profit/current_premium) are NOT touched or hidden -- it still
    appears in `trades`, unchanged -- only the aggregates
    (active_auto_paper_trades/total_locked_profit) and the
    virtual_trailing_status bucket now exclude/relabel it.

    This test exercises the real ti_store lifecycle (open_trade -> real
    market moves -> close_trade), not a synthetic upsert_state() like
    every other test in this file, specifically so it reproduces the same
    path production hit."""

    def test_orphaned_trade_is_tagged_and_excluded_from_active_aggregates(self, ti_db, monkeypatch):
        vt.init_db()
        monkeypatch.setattr(sysadmin_store, "get_agent_status", lambda agent: None)
        monkeypatch.setattr(vt, "current_premium", lambda row: 999.0)   # simulates the market moving on, live

        trade_id = ti_store.open_trade(
            symbol="CRUDEOILM", strike=7800, direction="CE", entry_price=129.6,
            target_price=149.04, sl_price=102.1, qty=1,
        )
        state = vt.evaluate_trade(
            vt._init_state(
                {"id": trade_id, "symbol": "CRUDEOILM", "direction": "CE", "entry_price": 129.6,
                 "sl_price": 102.1, "target_price": 149.04, "strike": 7800},
                now="2026-08-14T20:13:00",
            ),
            142.7,   # gain=13.1 -> TRAILING tier engaged, locked_profit > 0, state stays ACTIVE
        )
        vt.upsert_state(state)
        assert state["state"] == "ACTIVE"

        # The real trade closes -- exactly what the production evidence shows
        # (ti_paper_trades.status='CLOSED', exit_time populated) while the
        # virtual shadow row above is left exactly as last computed.
        ti_store.close_trade(trade_id, exit_price=132.9, exit_reason="TARGET HIT")

        snap = mc.get_control_center_snapshot()
        row = next(t for t in snap["trades"] if t["trade_id"] == trade_id)

        # Tagged, not hidden: the row still appears with its real, frozen
        # data untouched.
        assert row["orphaned"] is True
        assert row["state"] == "ACTIVE"   # the underlying frozen field is unchanged -- still virtual_trailing.py's own
        assert row["current_premium"] == 999.0
        assert row["highest_premium"] == state["highest_premium"]
        assert row["locked_profit"] == state["locked_profit"]

        # No longer inflates the "currently active" aggregates.
        assert snap["active_auto_paper_trades"] == 0
        assert snap["total_locked_profit"] == 0.0
        assert snap["virtual_trailing_status"].get("ORPHANED") == 1

    def test_a_genuinely_open_trade_is_not_tagged_orphaned(self, ti_db, monkeypatch):
        vt.init_db()
        monkeypatch.setattr(sysadmin_store, "get_agent_status", lambda agent: None)
        monkeypatch.setattr(vt, "current_premium", lambda row: 113.0)

        trade_id = ti_store.open_trade(
            symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
            target_price=120.0, sl_price=90.0, qty=1,
        )
        state = vt.evaluate_trade(
            vt._init_state(
                {"id": trade_id, "symbol": "NIFTY", "direction": "CE", "entry_price": 100.0,
                 "sl_price": 90.0, "target_price": 120.0, "strike": 24500},
                now="2026-08-14T09:15:00",
            ),
            113.0,
        )
        vt.upsert_state(state)

        snap = mc.get_control_center_snapshot()
        row = next(t for t in snap["trades"] if t["trade_id"] == trade_id)

        assert row["orphaned"] is False
        assert snap["active_auto_paper_trades"] == 1
        assert snap["total_locked_profit"] == state["locked_profit"]

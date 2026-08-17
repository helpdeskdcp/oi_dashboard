import datetime as dt

from agents.trading_intelligence import trade_guardian, trade_guardian_graph, trade_guardian_store
from test_agents.trading_intelligence.conftest import insert_cycle, insert_strike

SYMBOL = "NATURALGAS"
STRIKE = 250
WALL_STRIKE = 260


def _today_ts(hh, mm):
    return dt.datetime.now().strftime("%Y-%m-%d") + f"T{hh:02d}:{mm:02d}:00"


def _register(**overrides):
    plan = dict(
        symbol=SYMBOL, strike=STRIKE, direction="CE", entry_price=9.20, quantity=1,
        original_sl=6.50, original_t1=11.0, original_t2=13.0, original_t3=15.0,
        entry_timestamp=_today_ts(9, 0), registered_by="test",
    )
    plan.update(overrides)
    return trade_guardian.register_position(**plan)


def _insert_session(db_path, underlyings):
    for i, u in enumerate(underlyings):
        hh, mm = 9 + i // 4, (i * 15) % 60
        prem = max(0.5, 9.0 + (u - underlyings[0]) * 0.55)
        cid = insert_cycle(db_path, symbol=SYMBOL, ts=_today_ts(hh, mm), underlying_ltp=u, atm=STRIKE, pcr=0.5, max_pain=WALL_STRIKE)
        insert_strike(db_path, cid, STRIKE, ce_ltp=prem, ce_oi=9_200_000 + i * 1000, ce_oi_chg=1000, ce_vol=28000 + i * 100, ce_signal="Neutral")
        insert_strike(db_path, cid, WALL_STRIKE, ce_ltp=4.0, ce_oi=31_700_000, ce_oi_chg=0, ce_signal="Neutral")


class TestGraphCompiles:
    def test_build_graph_returns_a_compiled_graph(self):
        graph = trade_guardian_graph._build_graph()
        assert graph is not None


class TestRunShadow:
    def test_run_shadow_produces_a_result_for_a_registered_position(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        out = trade_guardian_graph.run_shadow(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert out["error"] is None
        assert out["result"] is not None
        assert out["result"].action in trade_guardian.ACTIONS
        assert "fetch_plan" in out["node_latencies"]
        assert "evaluate" in out["node_latencies"]
        assert "risk_gate" in out["node_latencies"]
        assert "decide_notification" in out["node_latencies"]

    def test_first_evaluation_always_notifies(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        out = trade_guardian_graph.run_shadow(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert out["notify"] is True

    def test_unchanged_result_does_not_notify_again(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        trade_guardian_graph.run_shadow(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        out2 = trade_guardian_graph.run_shadow(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert out2["notify"] is False
        assert "no meaningful change" in out2["notify_reason"]

    def test_changed_action_triggers_a_fresh_notification(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        trade_guardian_graph.run_shadow(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        out2 = trade_guardian_graph.run_shadow(pid, broker_position={"ltp": 25.0, "net_qty": 1})  # big profit -> TRAIL
        assert out2["notify"] is True


class TestFailureIsolation:
    def test_one_node_failure_does_not_stop_the_graph(self, ti_db, monkeypatch):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        monkeypatch.setattr(
            trade_guardian_graph.trade_guardian_store, "get_state",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        out = trade_guardian_graph.run_shadow(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert out["error"] is None
        assert "fetch_plan" in out["node_errors"]
        assert "boom" in out["node_errors"]["fetch_plan"]
        # downstream nodes still ran and produced a real result.
        assert out["result"] is not None

    def test_total_graph_failure_never_raises(self, ti_db, monkeypatch):
        def _boom():
            raise RuntimeError("graph construction exploded")
        monkeypatch.setattr(trade_guardian_graph, "_build_graph", _boom)
        out = trade_guardian_graph.run_shadow("anything", broker_position={"ltp": 1.0, "net_qty": 1})
        assert out["error"] is not None
        assert "graph construction exploded" in out["error"]
        assert out["result"] is None


class TestRiskGateOverride:
    def test_risk_gate_rejects_a_widened_sl(self, ti_db, monkeypatch):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        original_evaluate = trade_guardian.evaluate_position

        def _tampered(position_id, *, broker_position=None):
            result = original_evaluate(position_id, broker_position=broker_position)
            result.smart_sl = 1.0  # simulate a bug that widened the SL (CE: lower = wider)
            return result

        monkeypatch.setattr(trade_guardian_graph.trade_guardian, "evaluate_position", _tampered)
        out = trade_guardian_graph.run_shadow(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert out["result"].smart_sl == 6.50  # forced back to the original
        assert out["result"].sl_action == "KEEP"
        assert "risk_gate override" in out["result"].reason

    def test_risk_gate_rejects_an_unrecognized_action(self, ti_db, monkeypatch):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        original_evaluate = trade_guardian.evaluate_position

        def _tampered(position_id, *, broker_position=None):
            result = original_evaluate(position_id, broker_position=broker_position)
            result.action = "PLACE NEW ORDER"  # not an allowed action
            return result

        monkeypatch.setattr(trade_guardian_graph.trade_guardian, "evaluate_position", _tampered)
        out = trade_guardian_graph.run_shadow(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        assert out["result"].action == "HOLD"

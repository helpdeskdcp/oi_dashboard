"""
Tests for the Trade Guardian production shadow-wiring layer: the pure
gating logic (trade_guardian.should_run_shadow_cycle()) and the graph-
orchestrated cycle+notify entrypoint (trade_guardian_graph.
run_shadow_cycle_and_notify()) that app.py's own background loop calls.

app.py's own new background-task code (_trade_guardian_shadow_loop,
the import-isolation guard around `from agents.trading_intelligence
import trade_guardian, trade_guardian_graph`) is DELIBERATELY not
imported or exercised here -- this project's own established rule
(see conftest.py's own docstring) is to never import app.py in a test
process, since it carries real broker-session machinery. That app.py
code is instead verified by direct code inspection (it mirrors
agents/trading_intelligence/api.py's own already-tested import-
isolation pattern exactly) and by production log verification after
deployment, the same way every other app.py background loop in this
codebase is verified -- none of them have a direct unit test either.
"""
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


class TestShouldRunShadowCycle:
    def test_flag_off_is_always_false(self):
        now = dt.datetime.now()
        assert trade_guardian.should_run_shadow_cycle(
            enabled=False, last_run_at=None, now=now, cadence_seconds=60,
        ) is False
        assert trade_guardian.should_run_shadow_cycle(
            enabled=False, last_run_at=now - dt.timedelta(hours=1), now=now, cadence_seconds=60,
        ) is False

    def test_flag_on_first_run_is_true(self):
        assert trade_guardian.should_run_shadow_cycle(
            enabled=True, last_run_at=None, now=dt.datetime.now(), cadence_seconds=60,
        ) is True

    def test_flag_on_within_cadence_is_false(self):
        now = dt.datetime.now()
        assert trade_guardian.should_run_shadow_cycle(
            enabled=True, last_run_at=now - dt.timedelta(seconds=10), now=now, cadence_seconds=60,
        ) is False

    def test_flag_on_past_cadence_is_true(self):
        now = dt.datetime.now()
        assert trade_guardian.should_run_shadow_cycle(
            enabled=True, last_run_at=now - dt.timedelta(seconds=61), now=now, cadence_seconds=60,
        ) is True

    def test_exactly_at_cadence_boundary_is_true(self):
        now = dt.datetime.now()
        assert trade_guardian.should_run_shadow_cycle(
            enabled=True, last_run_at=now - dt.timedelta(seconds=60), now=now, cadence_seconds=60,
        ) is True


class TestBuildTelegramPayload:
    def test_includes_both_original_and_smart_values(self, ti_db):
        pid = _register()
        plan = trade_guardian_store.get_plan(pid)
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        result = trade_guardian.evaluate_position(pid, broker_position={"ltp": 9.55, "net_qty": 1})
        payload = trade_guardian.build_telegram_payload(plan, result)
        assert payload["original_sl"] == 6.50
        assert payload["original_target"] == 11.0
        assert payload["smart_sl"] == result.smart_sl
        assert payload["smart_target_low"] == result.smart_target_low
        assert payload["action"] == result.action


class TestRunShadowCycleAndNotify:
    def test_notifies_telegram_on_first_evaluation(self, ti_db, monkeypatch):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        sent = []
        monkeypatch.setattr(trade_guardian_graph.telegram_notifier, "send_trade_guardian_update",
                             lambda payload: sent.append(payload) or True)

        broker_positions = [{"symbol": "NATURALGAS250CE", "ltp": 9.55, "net_qty": 1}]
        results = trade_guardian_graph.run_shadow_cycle_and_notify(broker_positions)

        assert len(results) == 1
        assert len(sent) == 1
        assert sent[0]["position_id"] == pid
        assert sent[0]["original_sl"] == 6.50

    def test_does_not_notify_when_no_meaningful_change(self, ti_db, monkeypatch):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        sent = []
        monkeypatch.setattr(trade_guardian_graph.telegram_notifier, "send_trade_guardian_update",
                             lambda payload: sent.append(payload) or True)

        broker_positions = [{"symbol": "NATURALGAS250CE", "ltp": 9.55, "net_qty": 1}]
        trade_guardian_graph.run_shadow_cycle_and_notify(broker_positions)  # first run -> notifies
        trade_guardian_graph.run_shadow_cycle_and_notify(broker_positions)  # unchanged -> should not

        assert len(sent) == 1

    def test_telegram_failure_does_not_break_the_cycle(self, ti_db, monkeypatch):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)

        def _raise(payload):
            raise ConnectionError("telegram down")
        monkeypatch.setattr(trade_guardian_graph.telegram_notifier, "send_trade_guardian_update", _raise)

        broker_positions = [{"symbol": "NATURALGAS250CE", "ltp": 9.55, "net_qty": 1}]
        results = trade_guardian_graph.run_shadow_cycle_and_notify(broker_positions)  # must not raise
        assert len(results) == 1
        assert results[0]["error"] is None

    def test_no_broker_position_match_yields_unknown_state_not_notify_crash(self, ti_db, monkeypatch):
        _register()
        sent = []
        monkeypatch.setattr(trade_guardian_graph.telegram_notifier, "send_trade_guardian_update",
                             lambda payload: sent.append(payload) or True)

        results = trade_guardian_graph.run_shadow_cycle_and_notify([])  # no broker positions at all
        assert len(results) == 1
        assert results[0]["result"].state == "UNKNOWN"
        assert "POSITION STATE UNKNOWN" in results[0]["result"].reason
        assert len(sent) == 1  # first-ever evaluation, still a "meaningful" first observation

    def test_no_registered_plans_returns_empty(self, ti_db):
        assert trade_guardian_graph.run_shadow_cycle_and_notify([]) == []


class TestMatchBrokerPositionReuse:
    def test_run_trade_guardian_cycle_and_run_shadow_cycle_match_identically(self, ti_db):
        pid = _register()
        _insert_session(ti_db, [254.0, 254.5, 255.0, 255.5, 256.0] * 3)
        broker_positions = [{"symbol": "NATURALGAS250CE", "ltp": 9.55, "net_qty": 1}]

        plain = trade_guardian.run_trade_guardian_cycle(broker_positions)
        graph = trade_guardian_graph.run_shadow_cycle(broker_positions)

        assert plain[0].position_id == pid
        assert graph[0]["result"].position_id == pid
        assert plain[0].state == graph[0]["result"].state

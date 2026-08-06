"""
test_agents/trading_supervisor/test_supervision_engine.py -- regression
tests for supervision_engine.verify(). market_state.assess/
conflict_detector/data_health are monkeypatched at the module level so
each test controls exactly what "market state" or "conflict" looks
like, without needing a real candle archive or option-chain history.
"""
import dataclasses

from agents.dev_agent.gates.base import GateResult, GateStatus
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.trading_supervisor import market_state, supervision_engine


def _clean_market_state(symbol="NIFTY", date="2026-05-04"):
    return market_state.MarketState(
        symbol=symbol, date=date, trend_range={"regime": "Trending", "adx": 30.0, "atr_14": 10.0},
        volatility={"level": "normal", "vix": 14.0, "percentile": 50.0},
        expiry={"status": "normal"}, event={"status": "normal"},
    )


def _passing_gate_results():
    return [GateResult(gate="risk_assessment", status=GateStatus.PASSED, summary="ok", details={})]


class TestRiskGateStatus:
    def test_missing_when_no_risk_gate_present(self):
        assert supervision_engine._risk_gate_status([]) == "missing"

    def test_passed_when_present_and_passed(self):
        assert supervision_engine._risk_gate_status(_passing_gate_results()) == "passed"

    def test_failed_when_present_and_failed(self):
        results = [GateResult(gate="risk_assessment", status=GateStatus.FAILED, summary="bad", details={})]
        assert supervision_engine._risk_gate_status(results) == "failed"


class TestVerify:
    def test_missing_risk_gate_is_rejected(self, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        monkeypatch.setattr(supervision_engine.market_state, "assess", lambda *a, **k: _clean_market_state())
        monkeypatch.setattr(
            supervision_engine.data_health, "check_feed_staleness",
            lambda *a, **k: supervision_engine.data_health.DataHealth(
                symbol="NIFTY", latest_cycle_ts="t", staleness_minutes=1.0, is_stale=False, note="fresh",
            ),
        )
        verdict = supervision_engine.verify(
            candidate_name="c", symbol="NIFTY", direction="long", strategy_family="f",
            gate_results=[], memory_store=store,
        )
        assert verdict.decision == "REJECTED"
        assert verdict.risk_gate_status == "missing"

    def test_clean_state_with_risk_passed_is_approved(self, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        monkeypatch.setattr(supervision_engine.market_state, "assess", lambda *a, **k: _clean_market_state())
        monkeypatch.setattr(
            supervision_engine.data_health, "check_feed_staleness",
            lambda *a, **k: supervision_engine.data_health.DataHealth(
                symbol="NIFTY", latest_cycle_ts="t", staleness_minutes=1.0, is_stale=False, note="fresh",
            ),
        )
        verdict = supervision_engine.verify(
            candidate_name="c", symbol="NIFTY", direction="long", strategy_family="f",
            gate_results=_passing_gate_results(), memory_store=store,
        )
        assert verdict.decision == "APPROVED"

    def test_conflicting_signal_downgrades_to_requires_review(self, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        store.record_parameter_set(
            strategy_name="other", symbol="NIFTY", parameters={"direction": "short"}, is_best=True,
        )
        monkeypatch.setattr(supervision_engine.market_state, "assess", lambda *a, **k: _clean_market_state())
        monkeypatch.setattr(
            supervision_engine.data_health, "check_feed_staleness",
            lambda *a, **k: supervision_engine.data_health.DataHealth(
                symbol="NIFTY", latest_cycle_ts="t", staleness_minutes=1.0, is_stale=False, note="fresh",
            ),
        )
        verdict = supervision_engine.verify(
            candidate_name="c", symbol="NIFTY", direction="long", strategy_family="f",
            gate_results=_passing_gate_results(), memory_store=store,
        )
        assert verdict.decision == "REQUIRES_REVIEW"
        assert len(verdict.conflicts) == 1

    def test_elevated_volatility_downgrades_to_requires_review(self, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        volatile_state = dataclasses.replace(_clean_market_state(), volatility={"level": "high", "vix": 30, "percentile": 95})
        monkeypatch.setattr(supervision_engine.market_state, "assess", lambda *a, **k: volatile_state)
        monkeypatch.setattr(
            supervision_engine.data_health, "check_feed_staleness",
            lambda *a, **k: supervision_engine.data_health.DataHealth(
                symbol="NIFTY", latest_cycle_ts="t", staleness_minutes=1.0, is_stale=False, note="fresh",
            ),
        )
        verdict = supervision_engine.verify(
            candidate_name="c", symbol="NIFTY", direction="long", strategy_family="f",
            gate_results=_passing_gate_results(), memory_store=store,
        )
        assert verdict.decision == "REQUIRES_REVIEW"

    def test_stale_feed_downgrades_to_requires_review(self, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        monkeypatch.setattr(supervision_engine.market_state, "assess", lambda *a, **k: _clean_market_state())
        monkeypatch.setattr(
            supervision_engine.data_health, "check_feed_staleness",
            lambda *a, **k: supervision_engine.data_health.DataHealth(
                symbol="NIFTY", latest_cycle_ts="t", staleness_minutes=100.0, is_stale=True, note="stale",
            ),
        )
        verdict = supervision_engine.verify(
            candidate_name="c", symbol="NIFTY", direction="long", strategy_family="f",
            gate_results=_passing_gate_results(), memory_store=store,
        )
        assert verdict.decision == "REQUIRES_REVIEW"

    def test_explanation_is_a_nonempty_human_readable_string(self, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        monkeypatch.setattr(supervision_engine.market_state, "assess", lambda *a, **k: _clean_market_state())
        monkeypatch.setattr(
            supervision_engine.data_health, "check_feed_staleness",
            lambda *a, **k: supervision_engine.data_health.DataHealth(
                symbol="NIFTY", latest_cycle_ts="t", staleness_minutes=1.0, is_stale=False, note="fresh",
            ),
        )
        verdict = supervision_engine.verify(
            candidate_name="c", symbol="NIFTY", direction="long", strategy_family="f",
            gate_results=_passing_gate_results(), memory_store=store,
        )
        assert "Trading Supervisor" in verdict.explanation

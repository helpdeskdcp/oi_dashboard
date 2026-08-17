from types import SimpleNamespace

import pytest

from agents.trading_intelligence import signal_graph, signal_graph_store
from test_agents.trading_intelligence.conftest import insert_realistic_chain


def _fake_snapshot(available=True):
    return SimpleNamespace(available=available, reason=None if available else "no data", atm=24500)


def _fake_recommendation(action="BUY CE", direction="CE", confidence=80):
    return SimpleNamespace(action=action, direction=direction, confidence=confidence)


class TestRunShadowReusesProvidedInputs:
    def test_reuses_snapshot_findings_recommendation_without_recomputing(self, ti_db, monkeypatch):
        # If run_shadow() tried to recompute any of these, it would need a
        # real DB/market_data call -- these monkeypatches make that raise,
        # proving the graph only ever reused what was already given.
        monkeypatch.setattr(signal_graph.market_data, "get_snapshot",
                             lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not refetch snapshot")))
        monkeypatch.setattr(signal_graph.institutional_intelligence, "analyze",
                             lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not re-run institutional sweep")))
        monkeypatch.setattr(signal_graph.ai_trading_engine, "evaluate",
                             lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not re-evaluate")))

        rec = _fake_recommendation()
        result = signal_graph.run_shadow(
            "NIFTY", snapshot=_fake_snapshot(), findings=[], recommendation=rec,
            real_engine_action="BUY CE", persist=False,
        )
        assert result["graph_action"] == "BUY CE"
        assert result["graph_direction"] == "CE"
        assert result["graph_confidence"] == 80
        assert result["agrees_with_real_engine"] is True
        assert result["error"] is None
        assert result["node_errors"] == {}

    def test_disagreement_flag_false_when_actions_differ(self, ti_db):
        rec = _fake_recommendation(action="NO_TRADE", direction=None, confidence=None)
        result = signal_graph.run_shadow(
            "NIFTY", snapshot=_fake_snapshot(), findings=[], recommendation=rec,
            real_engine_action="BUY CE", persist=False,
        )
        assert result["graph_action"] == "NO_TRADE"
        assert result["agrees_with_real_engine"] is False

    def test_real_engine_action_none_leaves_agreement_none(self, ti_db):
        rec = _fake_recommendation()
        result = signal_graph.run_shadow(
            "NIFTY", snapshot=_fake_snapshot(), findings=[], recommendation=rec, persist=False,
        )
        assert result["agrees_with_real_engine"] is None


class TestRunShadowComputesWhenNotProvided:
    def test_computes_fresh_from_a_real_snapshot(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        result = signal_graph.run_shadow("NIFTY", persist=False)
        assert result["data_available"] is True
        assert result["graph_action"] in ("BUY CE", "BUY PE", "HOLD", "NO_TRADE")
        assert result["error"] is None
        assert "fetch_market_state" in result["node_latencies"]
        assert "score_and_decide" in result["node_latencies"]

    def test_unavailable_snapshot_degrades_honestly(self, ti_db):
        result = signal_graph.run_shadow("NOT_A_REAL_SYMBOL", persist=False)
        assert result["data_available"] is False
        assert result["graph_action"] == "NO_TRADE"
        assert result["regime_trend"] is None
        assert result["timeframe_alignment_score"] is None


class TestFailureIsolation:
    def test_one_node_failure_does_not_stop_the_graph(self, ti_db, monkeypatch):
        monkeypatch.setattr(
            signal_graph.regime_profile, "classify",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        rec = _fake_recommendation()
        result = signal_graph.run_shadow(
            "NIFTY", snapshot=_fake_snapshot(), findings=[], recommendation=rec, persist=False,
        )
        assert result["error"] is None
        assert "detect_regime" in result["node_errors"]
        assert "boom" in result["node_errors"]["detect_regime"]
        # Downstream nodes still ran and the graph still reached a decision.
        assert result["graph_action"] == "BUY CE"

    def test_total_graph_failure_never_raises(self, ti_db, monkeypatch):
        def _boom():
            raise RuntimeError("graph construction exploded")
        monkeypatch.setattr(signal_graph, "_build_graph", _boom)
        result = signal_graph.run_shadow("NIFTY", persist=False)
        assert result["error"] is not None
        assert "graph construction exploded" in result["error"]
        assert result["graph_action"] is None


class TestPersistence:
    def test_persist_true_writes_a_row(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        rec = _fake_recommendation()
        signal_graph.run_shadow(
            "NIFTY", snapshot=_fake_snapshot(), findings=[], recommendation=rec, persist=True,
        )
        rows = signal_graph_store.recent(limit=5)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "NIFTY"
        assert rows[0]["graph_action"] == "BUY CE"

    def test_persist_false_writes_nothing(self, ti_db):
        rec = _fake_recommendation()
        signal_graph.run_shadow(
            "NIFTY", snapshot=_fake_snapshot(), findings=[], recommendation=rec, persist=False,
        )
        assert signal_graph_store.recent(limit=5) == []

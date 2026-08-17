from agents.trading_intelligence import signal_graph_store


class TestRecordAndRecent:
    def test_record_then_recent_roundtrip(self, ti_db):
        row_id = signal_graph_store.record({
            "symbol": "NIFTY", "data_available": True, "regime_trend": "TRENDING",
            "regime_volatility": "NORMAL", "institutional_finding_count": 2,
            "graph_action": "BUY CE", "graph_direction": "CE", "graph_confidence": 78,
            "timeframe_alignment_score": 66.7, "timeframe_alignment_label": "MIXED",
            "real_engine_action": "BUY CE", "agrees_with_real_engine": True,
            "total_latency_ms": 12.3, "node_latencies": {"fetch_market_state": 1.1},
            "node_errors": None, "error": None,
        })
        assert row_id > 0
        rows = signal_graph_store.recent(limit=5)
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "NIFTY"
        assert row["graph_action"] == "BUY CE"
        assert row["agrees_with_real_engine"] == 1
        assert "fetch_market_state" in row["node_latencies_json"]

    def test_recent_orders_newest_first(self, ti_db):
        for i, symbol in enumerate(["NIFTY", "BANKNIFTY", "SENSEX"]):
            signal_graph_store.record({"symbol": symbol, "data_available": True})
        rows = signal_graph_store.recent(limit=10)
        assert [r["symbol"] for r in rows] == ["SENSEX", "BANKNIFTY", "NIFTY"]

    def test_missing_optional_fields_default_safely(self, ti_db):
        row_id = signal_graph_store.record({"symbol": "GOLD", "data_available": False})
        assert row_id > 0
        row = signal_graph_store.recent(limit=1)[0]
        assert row["graph_action"] is None
        assert row["agrees_with_real_engine"] is None

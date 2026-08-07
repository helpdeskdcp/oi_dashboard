from agents.trading_intelligence import data_access as da
from test_agents.trading_intelligence.conftest import insert_cycle, insert_strike


class TestLatestCycle:
    def test_returns_none_when_never_logged(self, ti_db):
        assert da.latest_cycle("NOT_A_REAL_SYMBOL") is None

    def test_returns_the_most_recent_cycle_with_strikes(self, ti_db):
        insert_cycle(ti_db, symbol="NIFTY", ts="2026-08-06T09:15:00")
        cid2 = insert_cycle(ti_db, symbol="NIFTY", ts="2026-08-06T09:18:00")
        insert_strike(ti_db, cid2, 24500)
        result = da.latest_cycle("NIFTY")
        assert result["cycle"]["ts"] == "2026-08-06T09:18:00"
        assert len(result["rows"]) == 1
        assert result["rows"][0].strike == 24500

    def test_null_greeks_coerce_to_dataclass_defaults_not_none(self, ti_db):
        """The real bug this sprint found: SQLite NULL for a never-
        written Greeks column must become StrikeRow's own 0.0 default,
        never a bare None (every oi_engine.py consumer assumes float)."""
        cid = insert_cycle(ti_db)
        insert_strike(ti_db, cid, 24500, ce_delta=None, ce_gamma=None)
        row = da.latest_cycle("NIFTY")["rows"][0]
        assert row.ce_delta == 0.0
        assert row.ce_gamma == 0.0

    def test_real_greeks_values_pass_through_unchanged(self, ti_db):
        cid = insert_cycle(ti_db)
        insert_strike(ti_db, cid, 24500, ce_delta=0.53, ce_gamma=0.0012)
        row = da.latest_cycle("NIFTY")["rows"][0]
        assert row.ce_delta == 0.53
        assert row.ce_gamma == 0.0012


class TestRecentCycles:
    def test_returns_newest_first(self, ti_db):
        insert_cycle(ti_db, ts="2026-08-06T09:15:00", pcr=1.0)
        insert_cycle(ti_db, ts="2026-08-06T09:18:00", pcr=1.1)
        cycles = da.recent_cycles("NIFTY")
        assert [c["pcr"] for c in cycles] == [1.1, 1.0]

    def test_respects_limit(self, ti_db):
        for i in range(5):
            insert_cycle(ti_db, ts=f"2026-08-06T09:{15+i}:00")
        assert len(da.recent_cycles("NIFTY", limit=3)) == 3


class TestRecentStrikeHistory:
    def test_returns_history_for_one_strike_only(self, ti_db):
        cid1 = insert_cycle(ti_db, ts="2026-08-06T09:15:00")
        insert_strike(ti_db, cid1, 24500, ce_oi=1000)
        insert_strike(ti_db, cid1, 24550, ce_oi=2000)
        cid2 = insert_cycle(ti_db, ts="2026-08-06T09:18:00")
        insert_strike(ti_db, cid2, 24500, ce_oi=1500)
        history = da.recent_strike_history("NIFTY", 24500)
        assert len(history) == 2
        assert all(h["strike"] == 24500 for h in history)


class TestLatestMarketStructure:
    def test_returns_none_when_no_snapshot(self, ti_db):
        assert da.latest_market_structure("NIFTY") is None

    def test_parses_json_fields(self, ti_db):
        import json
        import sqlite3
        conn = sqlite3.connect(ti_db)
        conn.execute(
            "INSERT INTO market_structure_snapshots (symbol, ts, vwap, liquidity_sweep_json) VALUES (?, ?, ?, ?)",
            ("NIFTY", "2026-08-06T10:00:00", 24480.5, json.dumps({"swept": "bullish", "reclaimed": True})),
        )
        conn.commit()
        conn.close()
        snapshot = da.latest_market_structure("NIFTY")
        assert snapshot["vwap"] == 24480.5
        assert snapshot["liquidity_sweep"]["swept"] == "bullish"


class TestLoadCandles:
    def test_loads_real_archived_candles(self, ti_db):
        candles = da.load_candles("NIFTY")
        assert not candles.empty
        assert list(candles.columns[:5]) == ["datetime", "open", "high", "low", "close"]

    def test_empty_dataframe_for_unknown_symbol(self, ti_db):
        candles = da.load_candles("NOT_A_REAL_SYMBOL")
        assert candles.empty

import datetime as dt
import sqlite3

from agents.trading_intelligence import institutional_intelligence as ii
from test_agents.trading_intelligence.conftest import (
    insert_cycle,
    insert_realistic_chain,
    insert_strike,
)


class TestAnalyze:
    def test_unavailable_when_never_logged(self, ti_db):
        result = ii.analyze("NOT_A_REAL_SYMBOL")
        assert result["available"] is False

    def test_detects_buildup_findings_from_stored_signals(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY")
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_signal='Long Buildup' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        result = ii.analyze("NIFTY", underlying=24505, expiry_date=dt.date.today() + dt.timedelta(days=2))
        buildups = [f for f in result["findings"] if f.pattern_type == "LongBuildup"]
        assert len(buildups) == 1
        assert buildups[0].strike == 24500

    def test_detects_oi_walls_among_a_realistic_chain(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        result = ii.analyze("NIFTY")
        walls = [f for f in result["findings"] if f.pattern_type == "OIWall"]
        assert len(walls) == 6  # top-3 support + top-3 resistance

    def test_detects_max_pain(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        result = ii.analyze("NIFTY")
        max_pain_findings = [f for f in result["findings"] if f.pattern_type == "MaxPainBehaviour"]
        assert len(max_pain_findings) == 1

    def test_detects_institutional_buying_after_elevated_oi_change(self, ti_db):
        # 10 quiet cycles, then one with a huge CE OI spike + volume spike -- Long Buildup.
        for i in range(10):
            cid = insert_cycle(ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{15+i}:00")
            insert_strike(ti_db, cid, 24500, ce_oi_chg=500, ce_vol=5000, ce_signal="Neutral")
        cid = insert_cycle(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00")
        insert_strike(ti_db, cid, 24500, ce_oi_chg=40000, ce_vol=60000, ce_signal="Long Buildup", ce_chg_pct=12.0)
        result = ii.analyze("NIFTY")
        institutional = [f for f in result["findings"] if f.pattern_type == "InstitutionalBuying"]
        assert len(institutional) == 1
        assert institutional[0].strike == 24500

    def test_no_institutional_flow_finding_for_ordinary_sized_moves(self, ti_db):
        for i in range(10):
            cid = insert_cycle(ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{15+i}:00")
            insert_strike(ti_db, cid, 24500, ce_oi_chg=500, ce_vol=5000, ce_signal="Long Buildup")
        result = ii.analyze("NIFTY")
        institutional = [f for f in result["findings"] if "Institutional" in f.pattern_type]
        assert institutional == []

    def test_institutional_flow_requires_2x_not_1_5x_expansion(self, ti_db):
        # 10 quiet cycles, then one at ~1.7x average OI change/volume -- above
        # compute_volume_expansion's own 1.5x default but below this module's
        # documented INSTITUTIONAL_OI_EXPANSION_MULT (2.0x). Must NOT fire;
        # regression guard for the fix that made both call sites actually pass
        # expansion_mult=INSTITUTIONAL_OI_EXPANSION_MULT instead of silently
        # using compute_volume_expansion's own 1.5x default.
        for i in range(10):
            cid = insert_cycle(ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{15+i}:00")
            insert_strike(ti_db, cid, 24500, ce_oi_chg=500, ce_vol=5000, ce_signal="Neutral")
        cid = insert_cycle(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00")
        insert_strike(ti_db, cid, 24500, ce_oi_chg=850, ce_vol=8500, ce_signal="Long Buildup", ce_chg_pct=12.0)
        result = ii.analyze("NIFTY")
        institutional = [f for f in result["findings"] if "Institutional" in f.pattern_type]
        assert institutional == []

    def test_gamma_trap_fires_near_expiry_near_atm_heavy_wall(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24500, atm=24500)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_oi=500000, ce_iv=18.0 WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        result = ii.analyze("NIFTY", underlying=24500, expiry_date=dt.date.today() + dt.timedelta(days=1))
        traps = [f for f in result["findings"] if f.pattern_type == "GammaTrap"]
        assert len(traps) >= 1

    def test_gamma_trap_does_not_fire_far_from_expiry(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24500, atm=24500)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_oi=500000, ce_iv=18.0 WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        result = ii.analyze("NIFTY", underlying=24500, expiry_date=dt.date.today() + dt.timedelta(days=30))
        traps = [f for f in result["findings"] if f.pattern_type == "GammaTrap"]
        assert traps == []

    def test_liquidity_sweep_reused_from_stored_snapshot(self, ti_db):
        import json
        insert_realistic_chain(ti_db, symbol="NIFTY")
        conn = sqlite3.connect(ti_db)
        conn.execute(
            "INSERT INTO market_structure_snapshots (symbol, ts, liquidity_sweep_json) VALUES (?, ?, ?)",
            ("NIFTY", "2026-08-06T10:00:00", json.dumps({"swept": "bullish", "reclaimed": True, "sweep_extreme": 24400})),
        )
        conn.commit()
        conn.close()
        result = ii.analyze("NIFTY")
        sweeps = [f for f in result["findings"] if f.pattern_type == "LiquiditySweep"]
        assert len(sweeps) == 1
        assert "bullish" in sweeps[0].description.lower()

    def test_no_liquidity_sweep_finding_without_a_snapshot(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        result = ii.analyze("NIFTY")
        assert [f for f in result["findings"] if f.pattern_type == "LiquiditySweep"] == []

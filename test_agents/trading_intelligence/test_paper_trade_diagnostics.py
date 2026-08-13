"""
test_agents/trading_intelligence/test_paper_trade_diagnostics.py --
Milestone 20, Phase 6: regression tests for
agents/trading_intelligence/paper_trade_diagnostics.py, the read-only
"why did today's paper trades win or lose" report backing
GET /api/papertrades/diagnostics.
"""
from agents.trading_intelligence import paper_trade_diagnostics as ptd
from agents.trading_intelligence import ti_store as ts


def _open_and_close(*, symbol="NIFTY", direction="CE", entry_time="2026-08-13T09:00:00",
                     points=100.0, confidence=50, risk_score=20, timeframe_alignment_score_at_entry=50.0):
    tid = ts.open_trade(
        symbol=symbol, strike=24500, direction=direction, entry_price=100.0, target_price=130.0,
        sl_price=85.0, qty=50, confidence=confidence, risk_score=risk_score,
        timeframe_alignment_score_at_entry=timeframe_alignment_score_at_entry,
    )
    # entry_time isn't a close_trade() param -- stamp it directly, matching
    # every other test file's convention for this table (ti_store.open_trade()
    # always uses _now(), so a fixed test date needs a direct UPDATE).
    import sqlite3
    conn = sqlite3.connect(ts.DB_PATH)
    conn.execute("UPDATE ti_paper_trades SET entry_time=? WHERE id=?", (entry_time, tid))
    conn.commit()
    conn.close()
    exit_price = 100.0 + points / 50.0
    ts.close_trade(tid, exit_price=exit_price, exit_reason="TARGET HIT" if points > 0 else "STOP LOSS")
    return tid


class TestComputeDiagnostics:
    def test_no_trades_reports_unavailable_not_a_fabricated_zero(self, ti_db):
        result = ptd.compute_diagnostics("2026-08-13")
        assert result["available"] is False
        assert result["trade_count"] == 0

    def test_basic_win_rate_and_expectancy(self, ti_db):
        _open_and_close(points=100.0)
        _open_and_close(points=-50.0)
        _open_and_close(points=-50.0)

        result = ptd.compute_diagnostics("2026-08-13")
        assert result["available"] is True
        assert result["trade_count"] == 3
        assert result["win_rate"] == round(1 / 3, 4)
        assert result["average_win"] == 100.0
        assert result["average_loss"] == -50.0
        assert result["expectancy"] == round((100.0 - 50.0 - 50.0) / 3, 2)

    def test_biggest_loser_is_the_most_negative_trade(self, ti_db):
        _open_and_close(symbol="NIFTY", direction="CE", points=-20.0)
        _open_and_close(symbol="SENSEX", direction="PE", points=-500.0)
        _open_and_close(symbol="GOLD", direction="CE", points=50.0)

        result = ptd.compute_diagnostics("2026-08-13")
        assert result["biggest_loser"] == "SENSEX PE"
        assert result["biggest_loser_points"] == -500.0

    def test_only_trades_on_the_requested_date_are_counted(self, ti_db):
        _open_and_close(entry_time="2026-08-13T09:00:00", points=100.0)
        _open_and_close(entry_time="2026-08-12T09:00:00", points=-500.0)

        result = ptd.compute_diagnostics("2026-08-13")
        assert result["trade_count"] == 1
        assert result["average_win"] == 100.0

    def test_against_structure_counts_contrary_timeframe_alignment(self, ti_db):
        _open_and_close(timeframe_alignment_score_at_entry=10.0)   # <= CONTRARY_ALIGNMENT_MAX
        _open_and_close(timeframe_alignment_score_at_entry=90.0)   # confirmed, not contrary

        result = ptd.compute_diagnostics("2026-08-13")
        assert result["against_structure"] == 1

    def test_low_confidence_trades_counted(self, ti_db):
        _open_and_close(confidence=20)   # <= LOW_CONFIDENCE_MAX (39)
        _open_and_close(confidence=90)

        result = ptd.compute_diagnostics("2026-08-13")
        assert result["low_confidence_trades"] == 1

    def test_oversized_stoploss_trades_counted(self, ti_db):
        _open_and_close(risk_score=85)   # >= OVERSIZED_STOPLOSS_RISK_SCORE_MIN (70)
        _open_and_close(risk_score=10)

        result = ptd.compute_diagnostics("2026-08-13")
        assert result["oversized_stoploss_trades"] == 1

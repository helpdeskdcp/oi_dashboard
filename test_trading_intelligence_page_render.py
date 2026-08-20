"""
test_trading_intelligence_page_render.py -- regression tests for the
2026-08-20 audit fixes to templates/trading_intelligence.html
(GET /admin/trading-intelligence):

1. A null-guard bug (total_ce_oi/total_pe_oi.toLocaleString() with no
   guard, unlike every sibling field in the same table) that could abort
   refreshOverview()'s entire 15s-cycle update for any symbol where the
   backend legitimately returns total_ce_oi/total_pe_oi as null/None.
2. Institutional Intelligence findings silently dropped their strike and
   evidence fields (both real, already-computed data).
3. Multi-Timeframe rendered only the latest close price, dropping
   open/high/low/bar_count/datetime (all already computed by
   get_multi_timeframe_summary()).
4. paper_trading.recent_closed_trades was fetched on every 15s poll and
   never rendered anywhere -- a new Recent Closed Trades table now shows it.

Same fixture pattern as test_performance_analytics.py (see that file's
own docstring for the route-level testing convention this repo uses).
"""
import os
import sqlite3

os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app
import auth
import billing
from agents import audit_log, event_bus
from agents.risk_manager import risk_store
from agents.runtime import runtime_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import data_access as ti_data_access, performance_analytics, ti_store, virtual_trailing
from agents.trading_supervisor import supervision_store

AGENT_MODULES = (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
    for mod in AGENT_MODULES:
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    monkeypatch.setattr(ti_data_access, "DB_PATH", db_path)
    monkeypatch.setattr(ti_store, "DB_PATH", db_path)
    monkeypatch.setattr(virtual_trailing, "DB_PATH", db_path)
    monkeypatch.setattr(performance_analytics, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _login_admin(client):
    conn = sqlite3.connect(app.DB_PATH)
    admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    conn.close()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_id


class TestPageStillRendersCleanly:
    def test_page_returns_200(self, client):
        _login_admin(client)
        resp = client.get("/admin/trading-intelligence")
        assert resp.status_code == 200


class TestNullGuardFix:
    """Bug: md.total_ce_oi.toLocaleString() with no guard, unlike every
    sibling field (md.atm ?? '—', md.pcr ?? '—', etc.) in the same table --
    a None value here would throw and abort the whole refreshOverview()
    cycle, not just this row."""

    def test_total_oi_fields_use_optional_chaining(self, client):
        _login_admin(client)
        html = client.get("/admin/trading-intelligence").get_data(as_text=True)
        assert "md.total_ce_oi?.toLocaleString()" in html
        assert "md.total_pe_oi?.toLocaleString()" in html
        # The old, unguarded form must be gone entirely.
        assert "md.total_ce_oi.toLocaleString()" not in html
        assert "md.total_pe_oi.toLocaleString()" not in html

    def test_oi_change_and_volume_fields_now_rendered(self, client):
        """These were computed by get_symbol_overview() (market_data dict)
        but never read anywhere in the template before this fix."""
        _login_admin(client)
        html = client.get("/admin/trading-intelligence").get_data(as_text=True)
        assert "md.total_ce_oi_change" in html
        assert "md.total_pe_oi_change" in html
        assert "md.volume_today" in html


class TestInstitutionalFindingsShowStrikeAndEvidence:
    def test_strike_and_evidence_columns_present(self, client):
        _login_admin(client)
        html = client.get("/admin/trading-intelligence").get_data(as_text=True)
        assert "f.strike" in html
        assert "f.evidence" in html


class TestMultiTimeframeShowsFullOhlc:
    def test_ohlc_and_bar_count_fields_present(self, client):
        _login_admin(client)
        html = client.get("/admin/trading-intelligence").get_data(as_text=True)
        assert "t.latest.open" in html
        assert "t.latest.high" in html
        assert "t.latest.low" in html
        assert "t.bar_count" in html
        assert "t.latest.datetime" in html


class TestRecentClosedTradesTable:
    """pt.recent_closed_trades was already being fetched on every 15s poll
    (get_paper_trading_summary() always returns it) but had zero render
    logic anywhere in the page -- purely additive, matches the same
    already-computed-never-displayed pattern as the Candle Freshness and
    Strategy Registry panels added earlier this session."""

    def test_table_markup_present(self, client):
        _login_admin(client)
        html = client.get("/admin/trading-intelligence").get_data(as_text=True)
        assert 'id="closed-trades-table"' in html

    def test_render_logic_reads_recent_closed_trades(self, client):
        _login_admin(client)
        html = client.get("/admin/trading-intelligence").get_data(as_text=True)
        assert "pt.recent_closed_trades" in html

"""Milestone 25 Workstream 2 regression tests -- the shared confidence-
metadata component (templates/_confidence_badge.html's Jinja macro, and
its JS counterpart static/js/confidence_badge.js) and its propagation
into dashboard.html/engine_v3.html/trading_intelligence.html.

The JS half is exercised via `node --check` + a functional smoke test in
CI tooling outside pytest's reach (this repo has no JS test runner) --
covered here only via static presence/wiring assertions. The Jinja macro
is fully unit-testable through Flask's own template environment, so that
half gets full behavioral coverage.
"""
import os
os.environ.setdefault("SKIP_AUTOSTART", "1")

import pytest

import app


@pytest.fixture()
def jinja_env():
    return app.app.jinja_env


def _render_badge(jinja_env, args_src):
    """`args_src` is raw Jinja call-argument source (e.g. "72" or
    "none, kind='calibrated_probability'") -- deliberately not built via
    repr() on Python values, since a test needs to express the bare
    Jinja keyword `none`, not the Python string "none"."""
    template = "{% import '_confidence_badge.html' as cb %}{{ cb.confidence_badge(" + args_src + ") }}"
    return jinja_env.from_string(template).render().strip()


class TestConfidenceBadgeMacroRawScore:
    def test_raw_score_always_carries_the_non_probability_caveat(self, jinja_env):
        html = _render_badge(jinja_env, "72")
        assert "72%" in html
        assert "not a win probability" in html
        assert "Confidence: 72%" in html

    def test_raw_score_none_value_shows_an_em_dash_not_a_fabricated_number(self, jinja_env):
        html = _render_badge(jinja_env, "none")
        assert "—" in html
        assert "not a win probability" in html

    def test_raw_score_empty_label_suppresses_the_redundant_prefix(self, jinja_env):
        html = _render_badge(jinja_env, "72, label=''")
        assert "Confidence:" not in html
        assert "72%" in html


class TestConfidenceBadgeMacroCalibratedProbability:
    def test_calibrated_probability_with_a_value_shows_percentage_and_sample_size(self, jinja_env):
        html = _render_badge(jinja_env, "65, kind='calibrated_probability', sample_size=12")
        assert "65%" in html
        assert "12 historical trade" in html
        assert "not a win probability" not in html   # this IS the calibrated one -- no raw-score caveat here

    def test_calibrated_probability_none_shows_honest_not_yet_calibrated_not_a_fabricated_number(self, jinja_env):
        html = _render_badge(
            jinja_env,
            "none, kind='calibrated_probability', "
            "note='insufficient history -- only 2 closed trade(s) in the 60-79 confidence bucket'",
        )
        assert "not yet calibrated" in html
        assert "65%" not in html
        assert "None" not in html
        assert "insufficient history" in html   # the real note surfaces via the title attribute

    def test_calibrated_probability_never_conflated_with_raw_confidence_language(self, jinja_env):
        html = _render_badge(jinja_env, "80, kind='calibrated_probability'")
        assert "Probability: 80%" in html
        assert "Confidence" not in html   # the word never appears -- distinct semantics, distinct label


class TestConfidenceBadgePropagatedIntoTemplates:
    """Confirms the templates that had bare, uncaveated confidence numbers
    (per the M25 audit finding) now actually include the shared component
    and call it at every previously-bare site -- not just that the macro
    itself works in isolation."""

    def test_engine_v3_includes_and_uses_the_shared_macro_at_all_three_sites(self):
        src = open("templates/engine_v3.html").read()
        assert "_confidence_badge.html" in src
        assert src.count("cb.confidence_badge(") == 3

    def test_dashboard_includes_the_js_component_and_uses_it_at_the_bare_sites(self):
        src = open("templates/dashboard.html").read()
        assert "js/confidence_badge.js" in src
        # ntm-confidence, sig.confidence (in the S/R detail line), and the
        # V3 decision line -- the three genuinely bare numeric-percentage
        # sites the M25 audit flagged.
        assert src.count("renderConfidenceBadge(") == 3
        # The ollama confidence_label site is categorical (HIGH/MEDIUM/LOW),
        # not numeric -- caveated via a title attribute instead of the
        # numeric badge component.
        assert "Rule-based signal-strength label, not a calibrated win probability" in src
        # The one deliberately-untouched site: an additive scoring
        # component correctly labeled "Signal-confirmation bonus" in its
        # own <div>, never presented as a standalone confidence claim.
        assert "Signal-confirmation bonus" in src

    def test_trading_intelligence_includes_and_uses_the_shared_component(self):
        src = open("templates/trading_intelligence.html").read()
        assert "js/confidence_badge.js" in src
        assert "renderConfidenceBadge({ value: rec.confidence, kind: 'raw_score'" in src
        assert "renderConfidenceBadge({ value: rec.probability, kind: 'calibrated_probability'" in src


class TestEngineV3RouteRendersCleanly:
    @pytest.fixture()
    def client(self, monkeypatch, tmp_path):
        import auth
        import billing
        db_path = str(tmp_path / "test.db")
        monkeypatch.setattr(app, "DB_PATH", db_path)
        monkeypatch.setattr(auth, "DB_PATH", db_path)
        monkeypatch.setattr(billing, "DB_PATH", db_path)
        monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
        monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
        monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
        app.init_db()
        app.app.config["TESTING"] = True
        with app.app.test_client() as c:
            yield c

    def _login_admin(self, client):
        import sqlite3
        conn = sqlite3.connect(app.DB_PATH)
        admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = admin_id

    def test_engine_v3_page_renders_without_a_jinja_error_with_no_v3_data_yet(self, client):
        self._login_admin(client)
        resp = client.get("/engine-v3")
        assert resp.status_code == 200

    def test_engine_v3_page_renders_the_confidence_badge_when_v3_data_is_present(self, client, monkeypatch):
        self._login_admin(client)
        symbol = next(iter(app.SYMBOLS.keys()))
        monkeypatch.setitem(app.state["v3_engine_enabled"], symbol, True)
        monkeypatch.setitem(app.state["v3_signal_by_symbol"], symbol, {
            "confidence": 72, "resistance_confidence": 65, "support_confidence": 58,
            "dynamic_resistance": None, "resistance_strike": None, "resistance_level_label": None,
            "next_resistance": None, "resistance_hold_probability": None, "resistance_break_probability": None,
            "dynamic_support": None, "support_strike": None, "support_level_label": None,
            "next_support": None, "support_hold_probability": None, "support_break_probability": None,
            "today_outcome": {}, "resistance_extend_up": None, "support_extend_down": None,
            "previous_day_validation": {},
            # sr_engine_v3.generate_v3_signal()'s remaining fields -- unrendered
            # by this specific test's assertions but required for engine_v3.html
            # to render at all without a Jinja UndefinedError (trade_decision/
            # reason are accessed unconditionally; tradeable must be True since
            # the primary Confidence badge this test checks for -- v3.confidence,
            # engine_v3.html:103 -- only renders inside `{% if v3.tradeable %}`;
            # its sibling fields there (suggested_entry/target/stop_loss/
            # risk_reward) are interpolated directly with no method calls, so
            # None is safe for them. resistance_cluster/support_cluster stay
            # None -- guarded by their own truthy checks.
            "computed_at": "10:00:00", "trade_decision": "BUY CE", "direction": "CE", "reason": "test reason",
            "regime_weights": None, "tradeable": True,
            "suggested_entry": None, "target": None, "stop_loss": None, "risk_reward": None,
            "resistance_cluster": None, "support_cluster": None,
        })
        resp = client.get("/engine-v3")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "not a win probability" in body
        assert "72%" in body

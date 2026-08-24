"""Regression tests for signal_qualification.py -- Signal Intelligence V2,
Stage B. Pure/monkeypatch-seam tests, matching test_regime_profile.py's own
established convention for controlling regime_profile._breakout_confirmation()
(and, here, failure_gate.run_failure_checks()) rather than re-testing their
own internals (already covered by test_failure_gate.py/test_regime_profile.py).
No DB fixture needed -- qualify_signal() itself never touches the DB
directly; the two seams above are where DB-backed calls would otherwise
happen."""
import datetime as dt

from oi_engine import StrikeRow

from agents.trading_intelligence import failure_gate as fg
from agents.trading_intelligence import regime_profile as rp
from agents.trading_intelligence import signal_qualification as sq


def _clear_failure_report(**overrides):
    checks = [
        fg.FailureCheck("reward_risk", fg.PASS, "reward:risk 2.0"),
        fg.FailureCheck("confidence", fg.PASS, "confidence 80 clears the tradeable threshold"),
        fg.FailureCheck("regime", fg.PASS, "regime supports this direction"),
        fg.FailureCheck("major_level_proximity", fg.PASS, "no hostile level nearby"),
    ]
    failed = [c.name for c in checks if c.status == fg.FAIL]
    report = fg.FailureReport(status=fg.STATUS_CLEAR if not failed else fg.STATUS_BLOCKED, checks=checks, failed=failed)
    for name, status in overrides.items():
        for c in report.checks:
            if c.name == name:
                c.status = status
        report.failed = [c.name for c in report.checks if c.status == fg.FAIL]
        report.status = fg.STATUS_BLOCKED if report.failed else fg.STATUS_CLEAR
    return report


def _rows(strike=24500, ce_signal="Short Covering", pe_signal="Neutral"):
    return [StrikeRow(strike=strike, ce_signal=ce_signal, pe_signal=pe_signal)]


def _base_kwargs(**overrides):
    kwargs = dict(
        symbol="NIFTY", direction="CE", strike=24500, entry_price=100.0, sl_price=90.0,
        target_price=120.0, confidence=80, probability=None, tradeable=True, rows=_rows(),
        atm=24500, underlying=24505.0, support=[], resistance=[], market_structure={"atr_14": 10.0},
        snapshot=None, expiry_date=dt.date.today() + dt.timedelta(days=2), expiry_context=None,
        is_mcx=False,
    )
    kwargs.update(overrides)
    return kwargs


class TestQualifySignalHardGates:
    def test_actionable_when_everything_clears(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report())
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        result = sq.qualify_signal(**_base_kwargs())
        assert result.production_action == sq.ACTIONABLE_BUY_CE
        assert result.production_confidence is not None

    def test_actionable_pe_direction(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report())
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        result = sq.qualify_signal(**_base_kwargs(direction="PE", rows=_rows(pe_signal="Short Covering")))
        assert result.production_action == sq.ACTIONABLE_BUY_PE

    def test_blocked_low_confidence_when_not_tradeable(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report(confidence=fg.FAIL))
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        result = sq.qualify_signal(**_base_kwargs(confidence=35, tradeable=False))
        assert result.production_action == sq.BLOCKED_LOW_CONFIDENCE

    def test_blocked_bad_regime(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report(regime=fg.FAIL))
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        result = sq.qualify_signal(**_base_kwargs())
        assert result.production_action == sq.BLOCKED_BAD_REGIME

    def test_blocked_risk_reward(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report(reward_risk=fg.FAIL))
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        result = sq.qualify_signal(**_base_kwargs())
        assert result.production_action == sq.BLOCKED_RISK_REWARD

    def test_blocked_no_level_confirmation_when_hostile_level_and_no_breakout(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report(major_level_proximity=fg.FAIL))
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["Volume expansion insufficient"]))
        result = sq.qualify_signal(**_base_kwargs())
        assert result.production_action == sq.BLOCKED_NO_LEVEL_CONFIRMATION

    def test_hostile_level_but_breakout_confirmed_is_not_blocked_on_level(self, monkeypatch):
        # A hostile level nearby is overridable by a genuinely confirmed
        # breakout -- same "breakout can override a chop/range read"
        # precedent regime_profile.classify_market_regime() already
        # establishes for its own breakout_override field.
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report(major_level_proximity=fg.FAIL))
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        result = sq.qualify_signal(**_base_kwargs())
        assert result.production_action != sq.BLOCKED_NO_LEVEL_CONFIRMATION

    def test_watchlist_when_breakout_not_confirmed_but_no_hard_gate_fails(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report())
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["Premium is not genuinely rising"]))
        result = sq.qualify_signal(**_base_kwargs())
        assert result.production_action == sq.WATCHLIST_CE


class TestVwapContradiction:
    def test_vwap_contradiction_with_no_other_confirmation_reduces_confidence_and_reasons(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report())
        monkeypatch.setattr(
            rp, "_breakout_confirmation",
            lambda *a, **kw: (False, ["Price is trading against VWAP", "Volume expansion insufficient"]),
        )
        result = sq.qualify_signal(**_base_kwargs())
        assert any("against VWAP with no" in r for r in result.reasons)
        assert result.production_confidence < 80  # base confidence was 80, must have been penalized

    def test_vwap_contradiction_alone_with_other_confirmation_is_not_penalized_the_same_way(self, monkeypatch):
        # Section 6: "do not automatically block every contradiction ...
        # instead require additional confirmation". If VWAP is the ONLY
        # failed check (volume/OI/momentum all independently confirmed),
        # that counts as confirmed despite the VWAP contradiction.
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report())
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["Price is trading against VWAP"]))
        result = sq.qualify_signal(**_base_kwargs())
        assert any("confirmed by breakout" in r for r in result.reasons)
        assert not any("no breakout/volume/OI/momentum confirmation" in r for r in result.reasons)


class TestOiEvidence:
    def test_oi_disagreeing_with_direction_is_penalized(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report())
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        # CE direction but PE side shows the bullish-for-PE signal (bearish for CE)
        result = sq.qualify_signal(**_base_kwargs(rows=_rows(ce_signal="Long Unwinding", pe_signal="Short Buildup")))
        assert any("disagrees with CE direction" in r for r in result.reasons)


class TestExpiryDayMode:
    def test_theta_decay_mode_is_recorded_as_evidence(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report())
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (True, []))
        result = sq.qualify_signal(**_base_kwargs(expiry_context={"theta_decay_mode": True, "gamma_risk_zone": True}))
        assert result.explanation["expiry_day_mode"] is True
        assert result.explanation["gamma_risk_zone"] is True

    def test_blocked_expiry_risk_when_gamma_zone_and_unconfirmed_and_low_confidence(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report())
        monkeypatch.setattr(rp, "_breakout_confirmation", lambda *a, **kw: (False, ["Volume expansion insufficient"]))
        result = sq.qualify_signal(**_base_kwargs(
            confidence=65, expiry_context={"theta_decay_mode": True, "gamma_risk_zone": True},
        ))
        assert result.production_action == sq.BLOCKED_EXPIRY_RISK


class TestNaturalgasSuccessCriteria:
    """The exact reported scenario (2026-08-24): NATURALGAS spot 264.4,
    VWAP 265.45 (spot below VWAP -- a contradiction for a BUY CE), ATM 260,
    confidence 35%, probability 25.8%, expiry today (theta_decay_mode),
    gamma HIGH, 'not near any major level ... low conviction ... skip
    unless everything else is very strong'. Must NOT resolve to
    ACTIONABLE_BUY_CE."""

    def test_naturalgas_weak_signal_is_never_actionable(self, monkeypatch):
        monkeypatch.setattr(fg, "run_failure_checks", lambda **kw: _clear_failure_report(confidence=fg.FAIL))
        monkeypatch.setattr(
            rp, "_breakout_confirmation",
            lambda *a, **kw: (False, ["Price is trading against VWAP", "Volume expansion insufficient"]),
        )
        result = sq.qualify_signal(
            symbol="NATURALGAS", direction="CE", strike=260, entry_price=5.25, sl_price=3.41,
            target_price=10.75, confidence=35, probability=25.8, tradeable=False,
            rows=_rows(strike=260, ce_signal="Neutral", pe_signal="Neutral"), atm=260, underlying=264.4,
            support=[], resistance=[], market_structure={"atr_14": 0.37, "vwap": 265.45},
            snapshot=None, expiry_date=dt.date.today(),
            expiry_context={"theta_decay_mode": True, "gamma_risk_zone": True, "expiry_pressure_score": 8},
            is_mcx=True,
        )
        assert result.production_action != sq.ACTIONABLE_BUY_CE
        assert result.production_action in (sq.BLOCKED_LOW_CONFIDENCE, sq.WATCHLIST_CE, sq.BLOCKED_EXPIRY_RISK)
        assert result.production_action == sq.BLOCKED_LOW_CONFIDENCE  # confidence 35 < 60 tradeable floor

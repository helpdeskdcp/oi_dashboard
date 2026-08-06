"""
test_agents/sys_admin/test_sysadmin_report.py -- regression tests for
SysAdminReport and build().
"""
import json

from agents.sys_admin import sysadmin_report


class TestBuild:
    def test_clamps_confidence_to_0_100(self):
        report = sysadmin_report.build(module="m", action="a", reason="r", confidence=150, evidence={"x": 1})
        assert report.confidence == 100
        report2 = sysadmin_report.build(module="m", action="a", reason="r", confidence=-10, evidence={"x": 1})
        assert report2.confidence == 0

    def test_defaults(self):
        report = sysadmin_report.build(module="m", action="a", reason="r", confidence=80, evidence={})
        assert report.affected_components == []
        assert report.recovery_outcome is None
        assert report.severity == "info"


class TestSysAdminReport:
    def test_to_json_round_trips(self):
        report = sysadmin_report.build(
            module="infra_monitor", action="disk_check", reason="disk usage high", confidence=90,
            evidence={"used_pct": 85.0}, affected_components=["oi_history.db"], severity="warning",
        )
        parsed = json.loads(report.to_json())
        assert parsed["module"] == "infra_monitor"
        assert parsed["evidence"]["used_pct"] == 85.0

    def test_human_readable_mentions_severity_and_confidence(self):
        report = sysadmin_report.build(
            module="m", action="a", reason="something happened", confidence=77, evidence={}, severity="critical",
        )
        text = report.human_readable()
        assert "CRITICAL" in text
        assert "77%" in text
        assert "something happened" in text

    def test_human_readable_includes_recovery_outcome_when_present(self):
        report = sysadmin_report.build(
            module="m", action="a", reason="r", confidence=50, evidence={}, recovery_outcome="cleared crashed flag",
        )
        assert "cleared crashed flag" in report.human_readable()

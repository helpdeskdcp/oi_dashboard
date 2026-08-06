"""
agents/dev_agent/gates/base.py -- shared result type for all five gates.
The pipeline treats PASSED and SKIPPED as non-blocking and FAILED as an
immediate stop; a gate only returns SKIPPED when it has a structural
reason not to apply (e.g. backtest_compare when no strategy file was
touched, or code_quality when no scanning tool is installed) -- never as
a way to avoid running.
"""
import dataclasses
import enum


class GateStatus(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclasses.dataclass
class GateResult:
    gate: str
    status: GateStatus
    summary: str
    details: dict = dataclasses.field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in (GateStatus.PASSED, GateStatus.SKIPPED)

    def to_dict(self) -> dict:
        return {
            "gate": self.gate, "status": self.status.value,
            "summary": self.summary, "details": self.details,
        }

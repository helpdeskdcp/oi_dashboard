"""
agents/runtime/metrics.py -- Milestone 15, Phase 3: Runtime Scheduler
Observability. Pure, side-effect-free aggregate computation -- no I/O,
no database, nothing here reads or writes anything.
"""


class RunningAverage:
    """Incremental mean -- O(1) per update, no history retained. This
    matches RuntimeScheduler's own existing footprint (it tracks only
    the LAST cycle's duration, never a list of past ones); average is
    now available too without growing that footprint into an
    unbounded list."""

    def __init__(self):
        self.count = 0
        self.total = 0.0

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value

    @property
    def average(self):
        if self.count == 0:
            return None
        return round(self.total / self.count, 2)

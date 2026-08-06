"""
agents/trading_supervisor/data_health.py -- "Detect abnormal behaviour,
stale data, API failures and inconsistent market feeds. Monitor broker
connectivity and data health."

Every signal here is an INDIRECT proxy (data staleness, audit-log
failure clustering) -- this module NEVER calls the live Angel One
session or anything that could trigger a real broker login, the same
landmine agents.risk_manager.data_access already documented and avoided
("hitting /live-positions in a test already triggered a real duplicate
broker login once," per this project's own history).
"""
import dataclasses
import datetime as dt


@dataclasses.dataclass
class DataHealth:
    symbol: str
    latest_cycle_ts: str | None
    staleness_minutes: float | None
    is_stale: bool
    note: str


class DataAccessError(Exception):
    """Raised only by latest_cycle_ts when the underlying read itself
    failed (DB unreachable, schema mismatch, ...) -- distinct from "no
    cycles found," which is a normal, expected empty result, not a
    failure. check_feed_staleness() catches this and reports it as a
    data-health finding rather than letting it propagate -- a module
    whose entire job is detecting abnormal conditions must not itself
    crash when one occurs."""


def latest_cycle_ts(symbol: str, *, as_of_date: str | None = None) -> str | None:
    import backtest
    date = as_of_date or dt.date.today().isoformat()
    try:
        cycles = backtest.load_cycles(symbol, date, date)
    except Exception as exc:
        raise DataAccessError(f"could not read cycles for {symbol}: {exc}") from exc
    if not cycles:
        return None
    return cycles[-1]["cycle"].get("ts")


def check_feed_staleness(symbol: str, *, staleness_minutes: int = 15,
                          now: dt.datetime | None = None, as_of_date: str | None = None) -> DataHealth:
    """Broker-connectivity proxy: if the live app is connected and
    polling normally, a new option-chain cycle lands in `cycles` every
    poll interval. A gap longer than staleness_minutes is the same
    symptom a real connectivity failure would produce -- checked
    indirectly, via already-logged data, never by asking the broker
    directly."""
    now = now or dt.datetime.now()
    try:
        ts = latest_cycle_ts(symbol, as_of_date=as_of_date)
    except DataAccessError as exc:
        return DataHealth(
            symbol=symbol, latest_cycle_ts=None, staleness_minutes=None, is_stale=True,
            note=f"could not read cycle data to check freshness: {exc}",
        )
    if ts is None:
        return DataHealth(
            symbol=symbol, latest_cycle_ts=None, staleness_minutes=None, is_stale=True,
            note="no cycle logged for this symbol today -- possible feed/connectivity failure, "
                 "or a symbol that hasn't traded yet",
        )
    try:
        age_minutes = (now - dt.datetime.fromisoformat(ts)).total_seconds() / 60
    except ValueError:
        return DataHealth(
            symbol=symbol, latest_cycle_ts=ts, staleness_minutes=None, is_stale=True,
            note="latest cycle timestamp could not be parsed",
        )
    is_stale = age_minutes > staleness_minutes
    note = (
        f"no new data for {round(age_minutes)} minutes -- exceeds the {staleness_minutes}-minute threshold"
        if is_stale else "feed is current"
    )
    return DataHealth(
        symbol=symbol, latest_cycle_ts=ts, staleness_minutes=round(age_minutes, 1), is_stale=is_stale, note=note,
    )


def failure_clustering(recent_audit_rows: list, *, window: int = 10, threshold: float = 0.6) -> dict:
    """"Inconsistent market feeds / abnormal behaviour" proxy at the
    audit-log level: if most of an agent's last `window` actions were
    rejected/failed, something systemic is more plausible than "every
    individual strategy independently happened to be bad" -- flagged
    for a human, never assumed to have any specific cause."""
    recent = recent_audit_rows[:window]
    if not recent:
        return {"clustered": False, "failure_rate": 0.0, "sample_size": 0}
    failures = sum(1 for r in recent if r.get("outcome") in ("rejected", "failed"))
    rate = failures / len(recent)
    return {"clustered": rate >= threshold, "failure_rate": round(rate, 2), "sample_size": len(recent)}

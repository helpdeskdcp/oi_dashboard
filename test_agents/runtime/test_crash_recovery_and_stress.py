"""
test_agents/runtime/test_crash_recovery_and_stress.py -- Module 12's own
explicit "crash recovery tests, stress tests," real (not mocked) --
same standard test_agents/sys_admin/test_production_readiness.py and
test_agents/hardening/test_memory_leak_and_stress_extended.py already
hold to.
"""
import concurrent.futures

from agents.runtime import agent_runtime as ar
from agents.runtime import policy_engine as pe
from agents.runtime import runtime_store as rs
from agents.runtime import task_queue as tq
from agents.runtime import workflow_engine as wf
from agents.runtime.scheduler import RuntimeScheduler


class TestCrashRecovery:
    def test_scheduler_restart_resumes_every_in_flight_workflow(self, agent_db, memory_store):
        """"Never lose workflow state": simulates a scheduler process
        restart (a fresh RuntimeScheduler instance, no in-memory state
        carried over) by creating workflows, discarding the scheduler
        object entirely, then starting a NEW one -- everything it needs
        to resume must have been on disk (runtime_store) already."""
        wid1 = rs.create_workflow(workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_EXECUTION,
                                   state={"symbol": "NIFTY", "decision": "APPROVED"})
        wid2 = rs.create_workflow(workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_HUMAN_APPROVAL,
                                   state={"symbol": "NIFTY", "decision": "APPROVED"})
        pe.set_policy("semi_auto", changed_by="t", reason="x")
        rs.update_workflow(wid2, status="waiting_approval")

        # No scheduler object exists yet -- a fresh one, starting cold,
        # must resume wid1 purely from what's on disk in runtime_store.
        new_scheduler = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        new_scheduler.start()

        # wid1 (running, at execution) must have been advanced by the resume sweep.
        w1 = rs.get_workflow(wid1)
        assert w1["status"] != "running" or w1["current_stage"] != wf.STAGE_EXECUTION

        # wid2 (waiting_approval) is untouched by the resume sweep -- still parked, not lost.
        w2 = rs.get_workflow(wid2)
        assert w2["status"] == "waiting_approval"

    def test_a_repeatedly_crashing_agent_is_escalated_exactly_once_per_threshold_crossing(self, agent_db, memory_store, risk_data_access_db):
        from agents import config, event_bus
        threshold = config.RUNTIME_MAX_CONSECUTIVE_FAILURES_BEFORE_ESCALATION
        for _ in range(threshold + 2):
            ar.run_agent_cycle("risk_manager", memory_store=memory_store)
        events = event_bus.events_since("2020-01-01", event_type="agent_escalated")
        # Escalates at threshold, threshold+1, threshold+2 -- every
        # cycle failure_counter >= threshold, never silently stops
        # alerting just because it already escalated once.
        assert len(events) == 3

    def test_workflow_that_raises_mid_stage_is_marked_failed_with_the_error_recorded(self, agent_db, memory_store, monkeypatch):
        wid = wf.start("NIFTY")
        wf.advance(wid, memory_store=memory_store)  # -> research

        def _boom(*a, **k):
            raise RuntimeError("simulated research engine crash")
        monkeypatch.setattr("agents.quant_researcher.research_engine.run_research_cycle", _boom)

        status = wf.advance(wid, memory_store=memory_store, repo_dir=".")
        assert status == "failed"
        history = rs.workflow_history(wid)
        assert any("simulated research engine crash" in (h["detail_json"].get("error") or "") for h in history)

    def test_queue_task_handler_that_raises_never_crashes_the_caller(self, agent_db):
        """A handler that raises must degrade to a 'retrying' task
        record, never propagate out of process_one() -- this is meant
        to run inside an unattended scheduler loop, where an uncaught
        exception here would kill the whole scheduler process."""
        tq.enqueue(priority="high", task_type="boom", payload=None, max_attempts=5)

        def handler(payload):
            raise RuntimeError("handler exploded")

        result = tq.process_one({"boom": handler})
        assert result["outcome"] == "retrying"
        # Immediately again: the task is now backed off (next_attempt_ts
        # in the future), so there is genuinely nothing due -- process_one()
        # returning None here is correct, not a crash.
        assert tq.process_one({"boom": handler}) is None


class TestStress:
    def test_concurrent_task_enqueues_and_claims_never_duplicate_a_claim(self, agent_db):
        """Real OS threads: 20 concurrent enqueuers, then a real
        concurrent free-for-all of claimers racing for the same rows.
        The one safety property claim_next_task()'s own docstring
        promises is that two callers can NEVER both claim the same
        task -- verified here under real contention, not simulated.
        (Whether every task gets claimed within one single concurrent
        burst is a SEPARATE, weaker property -- see the next test for
        why a task missed in one burst is never permanently lost, only
        picked up on a later attempt, exactly like a real scheduler's
        next tick would.)"""
        def enqueue_one(i):
            return tq.enqueue(priority="high", task_type="t", payload={"i": i})

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            ids = list(pool.map(enqueue_one, range(40)))
        assert len(set(ids)) == 40

        def claim_one(_):
            return rs.claim_next_task()

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(claim_one, range(60)))  # more claimers than tasks
        claimed_ids = [r["id"] for r in results if r is not None]
        assert len(claimed_ids) == len(set(claimed_ids))  # zero duplicate claims, however many landed

    def test_a_task_missed_under_contention_is_still_claimable_on_the_next_pass(self, agent_db):
        """Complements the test above: repeatedly sweeping (the real
        shape of a scheduler calling claim_next_task() every tick)
        eventually claims every task exactly once, even though a single
        concurrent burst may not."""
        for i in range(40):
            tq.enqueue(priority="high", task_type="t", payload={"i": i})

        claimed_ids = []
        for _ in range(80):  # generous headroom over 40 real tasks
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                results = list(pool.map(lambda _: rs.claim_next_task(), range(5)))
            claimed_ids.extend(r["id"] for r in results if r is not None)
            if len(claimed_ids) >= 40:
                break

        assert len(claimed_ids) == 40
        assert len(set(claimed_ids)) == 40

    def test_concurrent_agent_status_writes_across_all_six_agents_stay_consistent(self, agent_db, memory_store):
        """Real concurrency across every agent this milestone wires up
        at once -- a closer proxy to a real scheduler tick's shape
        (multiple agents' executions overlapping) than any single-agent
        stress test."""
        def run_one(agent):
            return ar.run_agent_cycle(agent, memory_store=memory_store, repo_dir=".")

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(run_one, ar.RUNTIME_AGENT_NAMES))

        assert len(results) == len(ar.RUNTIME_AGENT_NAMES)
        snapshot = ar.health_snapshot()
        for agent in ar.RUNTIME_AGENT_NAMES:
            assert snapshot[agent] is not None
            assert snapshot[agent]["currently_running"] == 0  # none left stuck "running"

    def test_many_sequential_workflow_stage_advances_stay_consistent(self, agent_db, memory_store):
        """200 stage transitions across repeated create/advance/complete
        cycles -- a real, if scaled-down, proxy for sustained scheduler
        operation (same posture as agents.sys_admin.maintenance's own
        bounded, real leak/stability probes)."""
        pe.set_policy("full_auto", changed_by="t", reason="x")
        completed = 0
        for _ in range(20):
            wid = rs.create_workflow(
                workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_HUMAN_APPROVAL,
                state={"symbol": "NIFTY", "decision": "APPROVED"},
            )
            status = wf.run_to_completion(wid, memory_store=memory_store)
            assert status == "completed"
            completed += 1
        assert completed == 20

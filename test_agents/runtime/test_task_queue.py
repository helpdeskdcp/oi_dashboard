import time

from agents.runtime import task_queue as tq


class TestProcessOne:
    def test_empty_queue_returns_none(self, agent_db):
        assert tq.process_one({}) is None

    def test_successful_handler_completes_the_task(self, agent_db):
        tq.enqueue(priority="high", task_type="ok", payload={"x": 1})
        result = tq.process_one({"ok": lambda payload: None})
        assert result["outcome"] == "completed"

    def test_raising_handler_retries(self, agent_db):
        tq.enqueue(priority="high", task_type="boom", payload=None, max_attempts=3)

        def handler(payload):
            raise RuntimeError("boom")

        result = tq.process_one({"boom": handler})
        assert result["outcome"] == "retrying"

    def test_unregistered_task_type_retries_with_a_clear_error(self, agent_db):
        tq.enqueue(priority="high", task_type="mystery", payload=None, max_attempts=3)
        result = tq.process_one({})
        assert result["outcome"] == "retrying"

    def test_timeout_is_enforced(self, agent_db):
        tq.enqueue(priority="high", task_type="slow", payload=None, max_attempts=3)

        def handler(payload):
            time.sleep(2)

        result = tq.process_one({"slow": handler}, timeout_seconds=0.1)
        assert result["timed_out"] is True
        assert result["outcome"] == "retrying"

    def test_exhausting_retries_lands_in_the_failed_queue(self, agent_db):
        tq.enqueue(priority="high", task_type="boom", payload=None, max_attempts=1)

        def handler(payload):
            raise RuntimeError("boom")

        result = tq.process_one({"boom": handler})
        assert result["outcome"] == "dead"
        assert any(t["task_type"] == "boom" for t in tq.failed_queue())


class TestQueueViews:
    def test_status_reflects_real_counts(self, agent_db):
        tq.enqueue(priority="high", task_type="t", payload=None)
        depth = tq.status()
        assert depth["queued"] == 1

    def test_retry_queue_lists_only_retrying_tasks(self, agent_db):
        tq.enqueue(priority="high", task_type="boom", payload=None, max_attempts=3)
        tq.process_one({"boom": lambda p: (_ for _ in ()).throw(RuntimeError("x"))})
        assert len(tq.retry_queue()) == 1

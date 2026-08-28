"""真实队列/存档 + 脚本模型：验证暂时失败恢复和终态边界，无外部 API。"""
import asyncio
import json
from datetime import date, datetime, timezone, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agent import agent as agent_module, default_registry
from app.agent.agent import Agent
from app.agent.checkpoint import decode_state, load_checkpoint
from app.config import settings
from app.db import write_session
from app.llm.errors import LLMCallError
from app.llm.mock import MockLLM
from app.models import AgentCheckpoint, AgentRun, Task, ToolCallLog
from app.tasks import queue, worker
from app import task_ownership

START, END = date(2015, 6, 1), date(2015, 6, 14)


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(
        time=lambda: value.now, perf_counter=lambda: value.now,
    ))
    monkeypatch.setattr(queue, "utcnow", lambda: datetime.fromtimestamp(value.now, timezone.utc).replace(tzinfo=None))
    monkeypatch.setattr(task_ownership, "utcnow", queue.utcnow)
    monkeypatch.setattr(settings, "AGENT_TOTAL_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(settings, "AGENT_STEP_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(settings, "AGENT_TOKEN_BUDGET", 1000)
    monkeypatch.setattr(settings, "TASK_RETRY_BACKOFF_SECONDS", 5.0)
    return value


class FailAfterMetric(MockLLM):
    """先正常取证，下一轮抛一次指定异常；后续按真实证据协议继续。"""
    def __init__(self, clock, failure):
        super().__init__()
        self.clock, self.failure = clock, failure
        self.calls, self.timeouts = 0, []
        self.inputs = []

    def chat(self, messages, *, json_mode=True, timeout=None, max_tokens=None):
        self.calls += 1
        self.timeouts.append(timeout)
        self.inputs.append(json.loads(json.dumps(messages)))
        self.clock.now += 1
        if self.calls == 2:
            raise self.failure
        return super().chat(messages, json_mode=json_mode, timeout=timeout)


def temporary_error(retry_after=0.0):
    return LLMCallError(kind="retryable", attempts=4, stop_reason="attempts_exhausted",
                        status_code=503, retry_after_seconds=retry_after)


def new_task(max_attempts=3):
    task = queue.create_task(payload={"item_id": 1, "start_date": str(START), "end_date": str(END)},
                             idempotency_key=uuid4().hex, max_retries=max_attempts)
    # 只操作本测试的任务，不领走其他测试留下的 pending 任务。
    with write_session() as session:
        row = session.get(Task, task.id)
        row.status, row.attempts = "running", 1
        row.lease_token = uuid4().hex
        row.lease_until = queue.utcnow() + timedelta(seconds=settings.TASK_LEASE_SECONDS)
        row.deadline_at = queue.utcnow() + timedelta(seconds=settings.AGENT_TOTAL_TIMEOUT_SECONDS)
        session.commit()
    return task.id


def process(task_id):
    asyncio.run(worker._process(task_id, asyncio.Semaphore(1)))


def claim_retry(task_id):
    with write_session() as session:
        row = session.get(Task, task_id)
        assert row.status == "retrying"
        assert row.retry_after <= queue.utcnow()
        row.status = "running"
        row.attempts += 1
        row.lease_token = uuid4().hex
        row.lease_until = queue.utcnow() + timedelta(seconds=settings.TASK_LEASE_SECONDS)
        session.commit()


def test_worker_resumes_failed_step_and_keeps_evidence_budget(clock, monkeypatch):
    llm = FailAfterMetric(clock, temporary_error())
    registry = default_registry()
    calls = []
    execute = registry.execute

    def record(tool, *args, **kwargs):
        calls.append(tool)
        return execute(tool, *args, **kwargs)

    registry.execute = record
    alerts = []
    monkeypatch.setattr(agent_module, "send_diagnosis_alert", lambda result: alerts.append(result))
    monkeypatch.setattr(worker, "Agent", lambda: Agent(llm=llm, registry=registry))
    task_id = new_task()
    process(task_id)
    task = queue.get_task(task_id)
    assert task.status == "retrying"
    assert task.finished_at is None
    assert alerts == []
    checkpoint = load_checkpoint(task.run_id)
    assert (checkpoint.status, checkpoint.step) == ("waiting_retry", 1)
    state = decode_state(checkpoint)
    assert state["next_step"] == 2
    assert state["tokens_in"] == state["tokens_out"] == 10
    assert state["llm_attempts"] == 5
    assert state["budget"]["elapsed_ms"] == 2000
    assert "metric#1" in state["investigation"]["evidence"]
    deadline = state["budget"]["deadline_at"]
    clock.now += 5
    claim_retry(task_id)
    process(task_id)
    finished = queue.get_task(task_id)
    assert finished.status == "succeeded"
    assert finished.run_id == task.run_id
    assert finished.error == "" and finished.retry_after is None
    assert calls.count("metric") == 1
    assert len(alerts) == 1
    result = json.loads(finished.result_json)
    assert result["budget"]["deadline_at"] == deadline
    assert result["llm_attempts"] == llm.calls + 3
    assert "metric#1" in result["evidence"]
    with write_session() as session:
        run = session.query(AgentRun).filter_by(run_id=task.run_id).one()
        assert run.status == "succeeded"
        assert run.tokens_in == (llm.calls - 1) * 10
        assert session.query(ToolCallLog).filter_by(run_id=task.run_id, tool="metric").count() == 1


@pytest.mark.parametrize("failure", [
    LLMCallError(kind="permanent", attempts=1, stop_reason="not_retryable", status_code=401),
    ValueError("private secret must not be copied"),
])
def test_permanent_or_unknown_errors_are_terminal(clock, monkeypatch, failure):
    llm = FailAfterMetric(clock, failure)
    monkeypatch.setattr(worker, "Agent", lambda: Agent(llm=llm))
    task_id = new_task()
    process(task_id)
    task = queue.get_task(task_id)
    assert task.status == "failed"
    assert task.retry_after is None
    checkpoint = load_checkpoint(task.run_id)
    assert checkpoint.status == "failed"
    assert "private secret" not in task.error + task.result_json + checkpoint.result_json
    cached = Agent(llm=llm).run(1, START, END, run_id=task.run_id, task_id=task_id)
    assert cached["status"] == "error"
    assert llm.calls == 2


def test_attempt_limit_closes_waiting_checkpoint(clock, monkeypatch):
    llm = FailAfterMetric(clock, temporary_error())
    monkeypatch.setattr(worker, "Agent", lambda: Agent(llm=llm))
    task_id = new_task(max_attempts=1)
    process(task_id)
    task = queue.get_task(task_id)
    assert task.status == "failed"
    assert json.loads(task.result_json)["task_stop_reason"] == "attempts_exhausted"
    assert load_checkpoint(task.run_id).status == "failed"
    assert Agent(llm=llm).run(1, START, END, run_id=task.run_id, task_id=task_id)["failure"]["retryable"] is False
    assert llm.calls == 2


def test_expired_deadline_does_not_reset_when_resuming(clock, monkeypatch):
    llm = FailAfterMetric(clock, temporary_error())
    monkeypatch.setattr(worker, "Agent", lambda: Agent(llm=llm))
    task_id = new_task()
    process(task_id)
    task = queue.get_task(task_id)
    clock.now += 61
    claim_retry(task_id)
    process(task_id)
    task = queue.get_task(task_id)
    assert task.status == "incomplete"
    assert json.loads(task.result_json)["stop_reason"] == "total_timeout"
    assert load_checkpoint(task.run_id).status == "stopped"
    assert llm.calls == 2


def test_server_wait_survives_gap_between_checkpoint_and_queue_update(clock):
    llm = FailAfterMetric(clock, temporary_error(retry_after=10.0))
    run_id = uuid4().hex
    result = Agent(llm=llm).run(1, START, END, run_id=run_id)
    assert result["failure"]["retryable"] is True
    clock.now += 3
    waiting = Agent(llm=llm).run(1, START, END, run_id=run_id)
    assert waiting["failure"]["retry_after_seconds"] == 7
    assert llm.calls == 2
    clock.now += 7
    assert Agent(llm=llm).run(1, START, END, run_id=run_id)["status"] == "ok"


def test_queue_does_not_schedule_wait_beyond_remaining_budget(clock, monkeypatch):
    monkeypatch.setattr(settings, "TASK_RETRY_BACKOFF_SECONDS", 100.0)
    llm = FailAfterMetric(clock, temporary_error())
    monkeypatch.setattr(worker, "Agent", lambda: Agent(llm=llm))
    task_id = new_task()
    process(task_id)
    task = queue.get_task(task_id)
    assert task.status == "failed"
    assert load_checkpoint(task.run_id).status == "failed"
    assert task.retry_after is None


def test_tokens_and_configuration_cannot_refresh_budget(clock, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_TOKEN_BUDGET", 25)
    llm = FailAfterMetric(clock, temporary_error())
    run_id = uuid4().hex
    Agent(llm=llm).run(1, START, END, run_id=run_id)
    monkeypatch.setattr(settings, "AGENT_TOKEN_BUDGET", 10000)
    result = Agent(llm=llm).run(1, START, END, run_id=run_id)
    assert result["stop_reason"] == "token_budget"
    assert result["budget"]["token_limit"] == 25
    assert result["tool_calls"] == 1
    assert load_checkpoint(run_id).status == "stopped"


def test_next_model_timeout_is_limited_by_original_deadline(clock):
    llm = FailAfterMetric(clock, temporary_error())
    run_id = uuid4().hex
    Agent(llm=llm).run(1, START, END, run_id=run_id)
    clock.now += 55  # 只剩 3 秒，不能重新得到 30 秒步骤预算。
    Agent(llm=llm).run(1, START, END, run_id=run_id)
    assert llm.timeouts[2] == 3.0


@pytest.mark.parametrize("change", [{"item_id": 2}, {"start": date(2015, 6, 2)}, {"task_id": 99}])
def test_terminal_checkpoint_identity_checked_before_cache_return(clock, change):
    run_id = uuid4().hex
    Agent(llm=MockLLM()).run(1, START, END, run_id=run_id)
    args = {"item_id": 1, "start": START, "end": END, "run_id": run_id, **change}
    with pytest.raises(ValueError, match="不一致"):
        Agent(llm=MockLLM()).run(**args)


@pytest.mark.parametrize("state_json", ["broken json", "{}", '{"schema_version":99}'])
def test_corrupt_or_old_active_checkpoint_never_silently_restarts(clock, state_json):
    run_id = uuid4().hex
    llm = FailAfterMetric(clock, temporary_error())
    Agent(llm=llm).run(1, START, END, run_id=run_id)
    with write_session() as session:
        row = session.query(AgentCheckpoint).filter_by(run_id=run_id).one()
        row.state_json = state_json
        session.commit()
    with pytest.raises(ValueError, match="格式不兼容或损坏"):
        Agent(llm=llm).run(1, START, END, run_id=run_id)
    assert llm.calls == 2


def test_complete_task_rejects_error_result(clock):
    task_id = new_task()
    with pytest.raises(ValueError, match="fail_task"):
        queue.complete_task(task_id, {"status": "error"})
    assert queue.get_task(task_id).status == "running"


def test_stale_attempt_cannot_finish_or_fail_new_attempt(clock):
    task_id = new_task()
    with write_session() as session:
        session.get(Task, task_id).attempts = 2
        session.commit()
    queue.complete_task(task_id, {"status": "ok"}, expected_attempt=1)
    queue.fail_task(task_id, "old failure", expected_attempt=1)
    assert queue.get_task(task_id).status == "running"


@pytest.mark.parametrize("result", [{}, {"status": "mystery"}, {"status": "error", "failure": {"retryable": "yes"}}, None])
def test_worker_rejects_unknown_or_malformed_results(clock, monkeypatch, result):
    task_id = new_task()
    monkeypatch.setattr(worker, "_run_diagnose", lambda task: result)
    process(task_id)
    assert queue.get_task(task_id).status == "failed"


def test_task_attempt_limit_uses_configuration(clock, monkeypatch):
    monkeypatch.setattr(settings, "TASK_MAX_RETRIES", 2)
    task = queue.create_task(idempotency_key=uuid4().hex)
    assert task.max_retries == 2


def test_repeated_model_failures_use_finite_task_attempts(clock, monkeypatch):
    class AlwaysUnavailable(MockLLM):
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            clock.now += 1
            raise temporary_error()

    llm = AlwaysUnavailable()
    monkeypatch.setattr(worker, "Agent", lambda: Agent(llm=llm))
    task_id = new_task(max_attempts=3)
    for attempt in range(1, 4):
        process(task_id)
        task = queue.get_task(task_id)
        assert task.attempts == attempt
        if attempt < 3:
            assert task.status == "retrying"
            clock.now = task.retry_after.replace(tzinfo=timezone.utc).timestamp()
            claim_retry(task_id)
        else:
            assert task.status == "failed"
    process(task_id)
    assert llm.calls == 3
    assert json.loads(task.result_json)["llm_attempts"] == 12
    assert load_checkpoint(task.run_id).status == "failed"


def test_worker_checks_claimed_attempt_before_starting_agent(clock, monkeypatch):
    task_id = new_task()
    with write_session() as session:
        session.get(Task, task_id).attempts = 2
        session.commit()

    def must_not_run(task):
        pytest.fail("old claimed attempt must not execute the new attempt")

    monkeypatch.setattr(worker, "_run_diagnose", must_not_run)
    asyncio.run(worker._process(task_id, asyncio.Semaphore(1), expected_attempt=1))
    assert queue.get_task(task_id).status == "running"


def test_expired_lease_cannot_renew_or_finish(clock):
    task_id = new_task()
    task = queue.get_task(task_id)
    owner = task_ownership.Ownership(task_id, task.lease_token, task.attempts)
    clock.now += settings.TASK_LEASE_SECONDS
    with task_ownership.use_owner(owner):
        with pytest.raises(task_ownership.OwnershipLost):
            task_ownership.renew_lease()
        queue.complete_task(task_id, {"status": "ok"})
        queue.fail_task(task_id, "late failure")
    assert queue.get_task(task_id).status == "running"


def test_heartbeat_renews_only_current_owner(clock):
    task_id = new_task()
    task = queue.get_task(task_id)
    clock.now += 10
    with task_ownership.use_owner(task_ownership.Ownership(task_id, task.lease_token, task.attempts)):
        task_ownership.renew_lease()
    renewed = queue.get_task(task_id)
    assert renewed.lease_until == task.lease_until + timedelta(seconds=10)
    assert renewed.heartbeat_at == queue.utcnow()
    queue.recover_stale_tasks(max_age_seconds=1)
    assert queue.get_task(task_id).status == "running"
    assert queue.get_task(task_id).lease_token == task.lease_token


def test_old_owner_cannot_write_checkpoint_report_or_task(clock, monkeypatch):
    from app.agent.checkpoint import save_checkpoint, complete_checkpoint
    from app.models import DiagnosticReport

    llm = FailAfterMetric(clock, temporary_error())
    monkeypatch.setattr(worker, "Agent", lambda: Agent(llm=llm))
    task_id = new_task()
    old_task = queue.get_task(task_id)
    old_owner = task_ownership.Ownership(task_id, old_task.lease_token, old_task.attempts)
    process(task_id)
    task = queue.get_task(task_id)
    checkpoint = load_checkpoint(task.run_id)
    with write_session() as session:
        original_report = session.query(DiagnosticReport).filter_by(run_id=task.run_id).one().content_json
    clock.now += 5
    claim_retry(task_id)
    with task_ownership.use_owner(old_owner):
        with pytest.raises(task_ownership.OwnershipLost):
            save_checkpoint(run_id=task.run_id, task_id=task_id, item_id=1, start=START, end=END,
                            anomaly_id=None, step=1, state=decode_state(checkpoint))
        with pytest.raises(task_ownership.OwnershipLost):
            complete_checkpoint(task.run_id, {"status": "ok"})
        with pytest.raises(task_ownership.OwnershipLost):
            Agent(llm=llm)._persist(
                task.run_id, 1, START, END, None, "final", 1, 0, 0, 0,
                0, 0, 0, "", {"conclusion": "stale"}, [], "succeeded", task_id)
        queue.complete_task(task_id, {"status": "ok"})
        queue.fail_task(task_id, "stale")
    assert queue.get_task(task_id).status == "running"
    assert load_checkpoint(task.run_id).state_json == checkpoint.state_json
    with write_session() as session:
        assert session.query(DiagnosticReport).filter_by(run_id=task.run_id).one().content_json == original_report


def test_lease_lost_during_model_call_prevents_tool_and_report(clock, monkeypatch):
    from app.models import DiagnosticReport
    task_id = new_task()

    class LoseLease(MockLLM):
        def chat(self, messages, **kwargs):
            response = super().chat(messages, **kwargs)
            with write_session() as session:
                session.get(Task, task_id).lease_token = uuid4().hex
                session.commit()
            return response

    registry = default_registry()
    monkeypatch.setattr(registry, "execute", lambda *a, **k: pytest.fail("失去租约后不应再调用工具"))
    monkeypatch.setattr(worker, "Agent", lambda: Agent(llm=LoseLease(), registry=registry))
    process(task_id)
    task = queue.get_task(task_id)
    assert task.status == "running"
    assert load_checkpoint(task.run_id).step == 0
    with write_session() as session:
        assert session.query(DiagnosticReport).filter_by(run_id=task.run_id).count() == 0


def test_worker_heartbeat_continues_during_blocking_diagnosis(clock, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    async def scenario():
        task_id = new_task()
        renewed = threading.Event()
        original_renew = worker.renew_lease

        def renew():
            original_renew()
            renewed.set()

        def diagnose(task):
            assert renewed.wait(2), "执行阻塞期间心跳被饿死"
            return {"status": "ok"}

        monkeypatch.setattr(worker, "renew_lease", renew)
        monkeypatch.setattr(worker, "_run_diagnose", diagnose)
        monkeypatch.setattr(settings, "TASK_HEARTBEAT_SECONDS", .01)
        with ThreadPoolExecutor(max_workers=1) as executor:
            await asyncio.wait_for(worker._process(task_id, asyncio.Semaphore(1), executor=executor), 3)
        assert queue.get_task(task_id).status == "succeeded"

    asyncio.run(scenario())


def test_heartbeat_db_failure_discards_late_result(clock, monkeypatch):
    import threading
    failed = threading.Event()
    task_id = new_task()

    def renew():
        failed.set()
        raise RuntimeError("DB unavailable with private connection info")

    def diagnose(task):
        assert failed.wait(2)
        assert task_ownership.current_owner.get().lost.wait(2)
        return {"status": "ok"}

    monkeypatch.setattr(worker, "renew_lease", renew)
    monkeypatch.setattr(worker, "_run_diagnose", diagnose)
    monkeypatch.setattr(settings, "TASK_HEARTBEAT_SECONDS", .01)
    process(task_id)
    assert queue.get_task(task_id).status == "running"


@pytest.fixture
def queued_task(clock):
    # 这些用例走真实 claim/recover，清空待办以免领到其他用例遗留任务。
    with write_session() as session:
        session.query(Task).delete()
        session.commit()
    return queue.create_task(payload={"item_id": 1, "start_date": str(START), "end_date": str(END)},
                             idempotency_key=uuid4().hex)


def test_crash_before_first_checkpoint_keeps_deadline_and_attempt_cap(clock, monkeypatch, queued_task):
    monkeypatch.setattr(settings, "TASK_LEASE_SECONDS", 3)
    with write_session() as session:
        session.get(Task, queued_task.id).max_retries = 2
        session.commit()
    first = queue.claim_pending(1)[0]
    deadline = first.deadline_at
    assert deadline == queue.utcnow() + timedelta(seconds=60)
    assert first.run_id
    clock.now += 3
    assert queue.recover_stale_tasks() == 1
    waiting = queue.get_task(first.id)
    assert waiting.status == "retrying" and waiting.attempts == 1
    assert waiting.lease_token is None
    assert queue.claim_pending(1) == []  # 仍需退避。
    clock.now += 5
    second = queue.claim_pending(1)[0]
    assert second.attempts == 2 and second.deadline_at == deadline
    assert second.run_id == first.run_id and second.lease_token != first.lease_token
    clock.now += 3
    assert queue.recover_stale_tasks() == 1
    stopped = queue.get_task(first.id)
    assert stopped.status == "failed" and stopped.attempts == 2
    assert json.loads(stopped.result_json)["task_stop_reason"] == "attempts_exhausted"
    assert queue.claim_pending(1) == []


def test_pending_wait_does_not_spend_budget_but_crash_recovery_cannot_refresh_it(clock, queued_task):
    clock.now += 1000  # 未首次领取不开始计算诊断预算。
    first = queue.claim_pending(1)[0]
    deadline = first.deadline_at
    clock.now += 61
    queue.recover_stale_tasks()
    stopped = queue.get_task(first.id)
    assert stopped.deadline_at == deadline
    assert stopped.status == "incomplete"
    assert json.loads(stopped.result_json)["stop_reason"] == "total_timeout"
    assert stopped.attempts == 1 and queue.claim_pending(1) == []


def test_claim_guard_rejects_manually_requeued_exhausted_task(clock, queued_task):
    task = queue.claim_pending(1)[0]
    with write_session() as session:
        row = session.get(Task, task.id)
        row.status, row.attempts = "pending", row.max_retries
        row.lease_token = row.lease_until = None
        session.commit()
    assert queue.claim_pending(1) == []
    assert queue.get_task(task.id).status == "failed"


@pytest.mark.parametrize("status", ["ok", "error", "incomplete"])
def test_recovery_syncs_terminal_checkpoint_without_new_attempt(clock, queued_task, status):
    task = queue.claim_pending(1)[0]
    with write_session() as session:
        row = session.get(Task, task.id)
        row.attempts = row.max_retries
        row.deadline_at = queue.utcnow() - timedelta(seconds=1)
        row.lease_until = queue.utcnow()
        session.add(AgentCheckpoint(run_id=task.run_id, task_id=task.id, item_id=1,
                                    window_start=START, window_end=END,
                                    status={"ok": "completed", "error": "failed", "incomplete": "stopped"}[status],
                                    result_json=json.dumps({"status": status, "run_id": task.run_id})))
        session.commit()
    assert queue.recover_stale_tasks() == 1
    settled = queue.get_task(task.id)
    assert settled.status == {"ok": "succeeded", "error": "failed", "incomplete": "incomplete"}[status]
    assert settled.attempts == settled.max_retries
    assert settled.lease_token is None and queue.claim_pending(1) == []


def test_crash_after_saved_step_resumes_evidence_once(clock, monkeypatch, queued_task):
    monkeypatch.setattr(settings, "TASK_LEASE_SECONDS", 5)
    task = queue.claim_pending(1)[0]

    class CrashAfterMetric(MockLLM):
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise SystemExit("simulated process loss")
            return super().chat(messages, **kwargs)

    with task_ownership.use_owner(task_ownership.Ownership(task.id, task.lease_token, task.attempts)):
        with pytest.raises(SystemExit):
            Agent(llm=CrashAfterMetric()).run(1, START, END, run_id=task.run_id, task_id=task.id)
    saved = load_checkpoint(task.run_id)
    assert saved.status == "active" and saved.step == 1
    clock.now += 5
    queue.recover_stale_tasks()
    clock.now += 5
    resumed = queue.claim_pending(1)[0]
    assert resumed.attempts == 2 and resumed.deadline_at == task.deadline_at
    process(task.id)
    assert queue.get_task(task.id).status == "succeeded"
    with write_session() as session:
        assert session.query(ToolCallLog).filter_by(run_id=task.run_id, tool="metric").count() == 1


def test_crash_recovery_preserves_server_wait(clock, monkeypatch, queued_task):
    monkeypatch.setattr(settings, "TASK_LEASE_SECONDS", 5)
    task = queue.claim_pending(1)[0]
    with task_ownership.use_owner(task_ownership.Ownership(task.id, task.lease_token, task.attempts)):
        Agent(llm=FailAfterMetric(clock, temporary_error(20.0))).run(
            1, START, END, run_id=task.run_id, task_id=task.id)
    not_before = decode_state(load_checkpoint(task.run_id))["retry_not_before"]
    clock.now += 5
    queue.recover_stale_tasks()
    waiting = queue.get_task(task.id)
    assert waiting.retry_after.replace(tzinfo=timezone.utc).timestamp() == not_before
    assert waiting.attempts == 1 and queue.claim_pending(1) == []
    clock.now = not_before
    assert queue.claim_pending(1)[0].attempts == 2


def test_recovery_rejects_corrupt_checkpoint_and_is_bounded(clock, queued_task):
    task = queue.claim_pending(1)[0]
    with write_session() as session:
        session.get(Task, task.id).lease_until = queue.utcnow()
        session.add(AgentCheckpoint(run_id=task.run_id, task_id=task.id, item_id=1,
                                    window_start=START, window_end=END, status="active", state_json="{}"))
        # 第二条旧任务本批不能顺便处理。
        session.add(Task(task_type="diagnose", status="running", idempotency_key=uuid4().hex))
        session.commit()
    assert queue.recover_stale_tasks(batch_size=1) == 1
    assert queue.get_task(task.id).error == "invalid_recovery_state"
    assert queue.recover_stale_tasks(batch_size=1) == 1
    assert queue.recover_stale_tasks(batch_size=1) == 0


def test_lease_expiring_while_waiting_for_db_lock_is_rejected(clock, monkeypatch):
    task_id = new_task()
    task = queue.get_task(task_id)
    before = task.lease_until - timedelta(seconds=1)
    after = task.lease_until + timedelta(seconds=1)
    times = iter([before, after])
    monkeypatch.setattr(task_ownership, "utcnow", lambda: next(times))
    with task_ownership.use_owner(task_ownership.Ownership(task_id, task.lease_token, task.attempts)):
        with pytest.raises(task_ownership.OwnershipLost, match="等待数据库锁"):
            task_ownership.renew_lease()
    assert queue.get_task(task_id).lease_until == task.lease_until


def test_heartbeat_cannot_keep_overdue_diagnosis_alive_forever(clock, queued_task):
    task = queue.claim_pending(1)[0]
    owner = task_ownership.Ownership(task.id, task.lease_token, task.attempts)
    with task_ownership.use_owner(owner):
        clock.now += 50
        task_ownership.renew_lease()  # 最后一次有效续租，租约可长于诊断截止时间。
        last_lease = queue.get_task(task.id).lease_until
        clock.now += 11
        with pytest.raises(task_ownership.OwnershipLost, match="截止时间"):
            task_ownership.renew_lease()
    assert queue.get_task(task.id).lease_until == last_lease
    clock.now = last_lease.replace(tzinfo=timezone.utc).timestamp()
    queue.recover_stale_tasks()
    assert queue.get_task(task.id).status == "incomplete"


def test_two_recovery_scanners_handle_expired_task_only_once(clock, queued_task):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    task = queue.claim_pending(1)[0]
    with write_session() as session:
        session.get(Task, task.id).lease_until = queue.utcnow()
        session.commit()
    barrier = Barrier(2)

    def recover():
        barrier.wait(timeout=2)
        return queue.recover_stale_tasks()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover) for _ in range(2)]
        assert sum(future.result(timeout=5) for future in futures) == 1
    assert queue.get_task(task.id).status == "retrying"
    assert queue.get_task(task.id).attempts == 1


def test_recovery_does_not_reuse_terminal_result_for_wrong_item(clock, queued_task):
    task = queue.claim_pending(1)[0]
    with write_session() as session:
        session.get(Task, task.id).lease_until = queue.utcnow()
        session.add(AgentCheckpoint(run_id=task.run_id, task_id=task.id, item_id=2,
                                    window_start=START, window_end=END, status="completed",
                                    result_json='{"status":"ok"}'))
        session.commit()
    queue.recover_stale_tasks()
    assert queue.get_task(task.id).error == "invalid_recovery_state"
    assert load_checkpoint(task.run_id).status == "completed"  # 保留异常原件供排查。


def test_corrupt_terminal_result_does_not_restart_model(clock):
    run_id = uuid4().hex
    Agent(llm=MockLLM()).run(1, START, END, run_id=run_id)
    with write_session() as session:
        session.query(AgentCheckpoint).filter_by(run_id=run_id).one().result_json = "{}"
        session.commit()
    with pytest.raises(ValueError, match="终态 checkpoint 结果损坏"):
        Agent(llm=MockLLM()).run(1, START, END, run_id=run_id)


def test_old_completed_error_is_terminal_not_silently_retried(clock):
    run_id = uuid4().hex
    with write_session() as session:
        session.add(AgentCheckpoint(run_id=run_id, item_id=1, window_start=START, window_end=END,
                                    status="completed", result_json='{"status":"error"}'))
        session.commit()
    assert Agent(llm=MockLLM()).run(1, START, END, run_id=run_id) == {"status": "error"}


def test_monitoring_does_not_count_retrying_or_incomplete_as_success():
    from app.monitoring import collect_monitoring
    from app.monitoring_history import collect_history

    before = collect_monitoring()["agent_runs"]
    errors_before = sum(b["agent_runs"]["error"] for b in collect_history()["buckets"])
    ids = [uuid4().hex for _ in range(6)]
    statuses = ["succeeded", "failed", "error", "incomplete", "retrying", "running"]
    with write_session() as session:
        for run_id, status in zip(ids, statuses):
            session.add(AgentRun(run_id=run_id, item_id=1, status=status))
        session.commit()
    try:
        after = collect_monitoring()["agent_runs"]
        assert after["total"] == before["total"] + 6
        assert after["succeeded"] == before["succeeded"] + 1
        assert after["error"] == before["error"] + 2
        assert after["retrying"] == before["retrying"] + 1
        assert after["incomplete"] == before["incomplete"] + 1
        errors_after = sum(b["agent_runs"]["error"] for b in collect_history()["buckets"])
        assert errors_after == errors_before + 2
    finally:
        with write_session() as session:
            session.query(AgentRun).filter(AgentRun.run_id.in_(ids)).delete()
            session.commit()

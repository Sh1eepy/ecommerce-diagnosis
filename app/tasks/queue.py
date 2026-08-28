"""DB 队列：任务创建、抢占、完成、失败重试（指数退避）。

幂等：idempotency_key 唯一索引，重复提交同一 key 返回已有任务。
抢占：SELECT ... FOR UPDATE SKIP LOCKED（MySQL 8 支持；SQLite 忽略该子句）。
优先级：priority 小的先执行；同优先级按创建时间。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy import update, or_
from uuid import uuid4

from app.db import write_session
from app.agent.checkpoint import terminal_status, decode_result, decode_state
from app.agent.state import FailureInfo, RunBudget
from app.config import settings
from app.models import AgentCheckpoint, AgentRun, Task, utcnow
from app.task_ownership import current_owner, lock_owned_task, OwnershipLost


def _lock_task(session, task_id: int, expected_attempt: int | None = None) -> Task | None:
    if current_owner.get() is not None:
        try:
            task = lock_owned_task(session, task_id)
        except OwnershipLost:
            return None
        return task if expected_attempt is None or task.attempts == expected_attempt else None
    # 仅兼容无租约的历史记录；缺少凭证不能修改新 Worker 领取的任务。
    conditions = [Task.id == task_id, Task.status == "running", Task.lease_token.is_(None)]
    if expected_attempt is not None:
        conditions.append(Task.attempts == expected_attempt)
    matched = session.execute(update(Task).where(*conditions).values(
        status=Task.status).execution_options(synchronize_session=False))
    return session.get(Task, task_id, populate_existing=True) if matched.rowcount == 1 else None


def create_task(
    task_type: str = "diagnose",
    payload: dict | None = None,
    anomaly_id: int | None = None,
    priority: int = 5,
    idempotency_key: str | None = None,
    max_retries: int | None = None,
) -> Task:
    """创建任务；同 idempotency_key 已存在则返回已有任务（幂等）。"""
    attempt_limit = settings.TASK_MAX_RETRIES if max_retries is None else max_retries
    if type(attempt_limit) is not int or not 1 <= attempt_limit <= 10:
        raise ValueError("任务总尝试次数必须为 1～10")
    idem = idempotency_key or f"{task_type}:{json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)}"
    with write_session() as s:
        existing = s.query(Task).filter_by(idempotency_key=idem).first()
        if existing:
            return existing
        task = Task(
            task_type=task_type,
            anomaly_id=anomaly_id,
            idempotency_key=idem,
            priority=priority,
            payload_json=json.dumps(payload or {}, ensure_ascii=False),
            max_retries=attempt_limit,
        )
        s.add(task)
        try:
            s.commit()
        except IntegrityError:
            # 两个请求并发通过前置查询时，唯一约束负责最终仲裁；失败方返回赢家记录。
            s.rollback()
            return s.query(Task).filter_by(idempotency_key=idem).one()
        s.refresh(task)
        return task


def ensure_task_run_id(task_id: int) -> str:
    """为任务持久化稳定 run_id；重试沿用同一 ID 才能命中 Agent checkpoint。"""
    from app.tracing import new_run_id

    with write_session() as s:
        task = _lock_task(s, task_id)
        if task is None:
            raise OwnershipLost("任务不存在或当前执行者没有领取权限")
        if not task.run_id:
            task.run_id = new_run_id()
            s.commit()
        return task.run_id


def _record_outcome(session, task, outcome, now, *, update_run=True):
    """调用者已锁住 Task；收尾与失效领取凭证在同一事务。"""
    task.status = {"ok": "succeeded", "error": "failed", "incomplete": "incomplete"}[outcome["status"]]
    task.result_json = json.dumps(outcome, ensure_ascii=False)
    task.finished_at = now
    task.retry_after = None
    task.lease_token = task.lease_until = None
    task.error = str(outcome.get("task_stop_reason") or outcome.get("stop_reason") or "") if task.status == "failed" else ""
    run = session.query(AgentRun).filter_by(run_id=task.run_id).first() if task.run_id and update_run else None
    if run is not None:
        run.status = task.status


def _stop_recovery(session, task, checkpoint, now, reason, *, incomplete=False):
    outcome = {"status": "incomplete" if incomplete else "error", "run_id": task.run_id,
               "stop_reason": reason if incomplete else "task_error", "task_stop_reason": reason}
    if not incomplete:
        outcome["failure"] = FailureInfo(kind="unknown", reason=reason).model_dump()
    # 保留可解码的已完成步骤计数/证据，绝不靠损坏存档重建一个零消耗预算。
    if checkpoint is not None and checkpoint.task_id == task.id:
        try:
            state = decode_state(checkpoint)
            outcome.update(budget=state["budget"], steps=state["steps"],
                           llm_attempts=state["llm_attempts"],
                           evidence=state["investigation"].get("evidence", {}))
        except ValueError:
            pass
        if checkpoint.status in {"active", "waiting_retry"} and checkpoint.task_id == task.id:
            checkpoint.status = terminal_status(outcome)
            checkpoint.result_json = json.dumps(outcome, ensure_ascii=False)
            checkpoint.updated_at = now
    _record_outcome(session, task, outcome, now,
                    update_run=checkpoint is None or checkpoint.task_id == task.id)


def _resume_allowed(session, task, now):
    """领取与回收共用：终态优先，再校验原始预算、次数和存档。返回 (允许, state)。"""
    checkpoint = session.query(AgentCheckpoint).filter_by(run_id=task.run_id).first() if task.run_id else None
    state = None
    try:
        if checkpoint is not None:
            payload = json.loads(task.payload_json)
            if (checkpoint.task_id != task.id or checkpoint.anomaly_id != task.anomaly_id
                    or checkpoint.window_start != date.fromisoformat(payload["start_date"])
                    or checkpoint.window_end != date.fromisoformat(payload["end_date"])
                    or (int(payload["item_id"]) != 0 and checkpoint.item_id != int(payload["item_id"]))):
                raise ValueError("checkpoint 不属于当前任务")
            outcome = decode_result(checkpoint)
            if outcome is not None:
                _record_outcome(session, task, outcome, now)
                return False, None  # 仅同步结果，不增加执行次数，也不受重跑预算限制。
            if checkpoint.status not in {"active", "waiting_retry"}:
                raise ValueError("不支持的 checkpoint 状态")
            state = decode_state(checkpoint)
            if checkpoint.status == "waiting_retry":
                waiting = json.loads(checkpoint.result_json)
                failure = FailureInfo.model_validate(waiting["failure"])
                if waiting.get("status") != "error" or failure.kind != "retryable" or not failure.retryable:
                    raise ValueError("等待重试状态不一致")
        if task.attempts < 0 or not 1 <= task.max_retries <= 10:
            raise ValueError("无效的任务次数限制")
        if task.deadline_at is None:
            if state is not None:
                task.deadline_at = datetime.fromtimestamp(state["budget"]["deadline_at"], timezone.utc).replace(tzinfo=None)
            elif task.attempts == 0 and task.status == "pending":
                task.deadline_at = now + timedelta(seconds=settings.AGENT_TOTAL_TIMEOUT_SECONDS)
            else:
                _stop_recovery(session, task, checkpoint, now, "recovery_metadata_missing")
                return False, None
        remaining = (task.deadline_at - now).total_seconds()
        if state is not None:
            budget = RunBudget.model_validate(state["budget"])
            saved_deadline = datetime.fromtimestamp(budget.deadline_at, timezone.utc).replace(tzinfo=None)
            task.deadline_at = min(task.deadline_at, saved_deadline)
            remaining = min((task.deadline_at - now).total_seconds(),
                            min(budget.seconds_limit, settings.AGENT_TOTAL_TIMEOUT_SECONDS) - budget.elapsed_ms / 1000)
            if remaining > 0 and state["tokens_in"] + state["tokens_out"] >= min(budget.token_limit, settings.AGENT_TOKEN_BUDGET):
                _stop_recovery(session, task, checkpoint, now, "token_budget", incomplete=True)
                return False, None
            if remaining > 0 and state["next_step"] > min(budget.max_steps, settings.AGENT_MAX_STEPS):
                _stop_recovery(session, task, checkpoint, now, "max_steps", incomplete=True)
                return False, None
        if remaining <= 0:
            _stop_recovery(session, task, checkpoint, now, "total_timeout", incomplete=True)
            return False, None
        if task.attempts >= task.max_retries:
            _stop_recovery(session, task, checkpoint, now, "attempts_exhausted")
            return False, None
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        _stop_recovery(session, task, checkpoint, now, "invalid_recovery_state")
        return False, None
    return True, state


def claim_pending(limit: int = 1) -> list[Task]:
    """抢占 pending 任务（含已到重试时间的 retrying）。"""
    if type(limit) is not int or limit < 1:
        raise ValueError("领取数量必须为正整数")
    now = utcnow()
    with write_session() as s:
        rows = (
            s.query(Task)
            .filter(
                Task.status.in_(["pending", "retrying"]),
                (Task.retry_after.is_(None)) | (Task.retry_after <= now),
            )
            .order_by(Task.priority, Task.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        claimed = []
        for t in rows:
            # 行锁之外再做条件更新，防止 SQLite 或并发变化导致重复领取。
            matched = s.execute(update(Task).where(
                Task.id == t.id, Task.status == t.status, Task.attempts == t.attempts,
                or_(Task.retry_after.is_(None), Task.retry_after <= now),
            ).values(status=Task.status)
              .execution_options(synchronize_session=False))
            if matched.rowcount != 1:
                continue
            s.refresh(t)
            allowed, state = _resume_allowed(s, t, now)
            if not allowed:
                continue
            if state is not None and state["retry_not_before"] > now.replace(tzinfo=timezone.utc).timestamp():
                if state["retry_not_before"] >= t.deadline_at.replace(tzinfo=timezone.utc).timestamp():
                    checkpoint = s.query(AgentCheckpoint).filter_by(run_id=t.run_id).first()
                    _stop_recovery(s, t, checkpoint, now, "total_timeout", incomplete=True)
                else:
                    wait_until = datetime.fromtimestamp(state["retry_not_before"], timezone.utc).replace(tzinfo=None)
                    t.status, t.retry_after = "retrying", wait_until
                continue
            t.status = "running"
            t.attempts += 1
            t.started_at = t.heartbeat_at = now
            t.lease_token = uuid4().hex
            t.lease_until = now + timedelta(seconds=settings.TASK_LEASE_SECONDS)
            t.run_id = t.run_id or uuid4().hex
            t.retry_after = None
            claimed.append(t)
        s.commit()
        for t in claimed:
            s.refresh(t)
        return claimed


def complete_task(task_id: int, result: dict, run_id: str | None = None,
                  *, expected_attempt: int | None = None) -> None:
    if result.get("status") not in {"ok", "incomplete"}:
        raise ValueError("错误/未知结果必须走 fail_task，不能当作完成")
    terminal_status(result)
    with write_session() as s:
        t = _lock_task(s, task_id, expected_attempt)
        if t is None:
            return
        if t.status != "running" or (expected_attempt is not None and t.attempts != expected_attempt):
            return  # 迟到的重复 Worker 不能覆盖已经确定的终态
        # Agent 证据不足/预算耗尽不等于队列执行成功，保留真实完成语义。
        t.status = "succeeded" if result.get("status") == "ok" else "incomplete"
        t.result_json = json.dumps(result, ensure_ascii=False)
        t.run_id = run_id or t.run_id
        t.finished_at = utcnow()
        t.retry_after = None
        t.error = ""
        t.lease_token = t.lease_until = None
        s.commit()


def fail_task(task_id: int, error: str, *, retryable: bool = False,
              retry_after_seconds: float = 0.0, result: dict | None = None,
              expected_attempt: int | None = None) -> None:
    """仅明确的临时故障允许回队；次数、等待时间和原始诊断预算共同约束。"""
    with write_session() as s:
        t = _lock_task(s, task_id, expected_attempt)
        if t is None:
            return
        if t.status != "running" or (expected_attempt is not None and t.attempts != expected_attempt):
            return
        t.error = str(error)[:2000]
        now = utcnow()
        delay = max(settings_task_backoff(t.attempts), retry_after_seconds)
        can_retry = retryable is True and t.attempts < t.max_retries
        if not math.isfinite(delay) or not math.isfinite(retry_after_seconds) or retry_after_seconds < 0 or delay > 86400:
            can_retry = False
        checkpoint = s.query(AgentCheckpoint).filter_by(run_id=t.run_id).first() if t.run_id else None
        if checkpoint is not None and checkpoint.task_id != t.id:
            _stop_recovery(s, t, checkpoint, now, "invalid_recovery_state")
            s.commit()
            return
        if checkpoint is not None and checkpoint.task_id == t.id and checkpoint.status in {"completed", "failed", "stopped"}:
            try:
                terminal = decode_result(checkpoint)
            except ValueError:
                terminal = None
            if terminal is not None:
                _record_outcome(s, t, terminal, now)
                s.commit()
                return  # 报告已完成后队列写入异常，不能把完整结果降成 Worker 失败。
        outcome = json.loads(json.dumps(result)) if result is not None else None
        can_retry = can_retry and t.deadline_at is not None and delay < (t.deadline_at - now).total_seconds()
        if can_retry and outcome is not None:
            try:
                budget = RunBudget.model_validate(outcome["budget"])
                remaining = min(budget.deadline_at - now.replace(tzinfo=timezone.utc).timestamp(),
                                budget.seconds_limit - budget.elapsed_ms / 1000)
                can_retry = delay < remaining
            except (KeyError, TypeError, ValueError):
                can_retry = False  # 缺失/损坏预算的错误结果不自动恢复。
        if not can_retry:
            t.status = "failed"
            t.finished_at = now
            t.retry_after = None
            if outcome is None:
                outcome = {"status": "error", "stop_reason": "task_error", "run_id": t.run_id}
            try:
                failure = FailureInfo.model_validate(outcome.get("failure"))
            except (TypeError, ValueError):
                failure = FailureInfo(kind="unknown", reason="task_error")
            failure.retryable = False
            outcome["failure"] = failure.model_dump()
            outcome["task_stop_reason"] = (
                "attempts_exhausted" if retryable and t.attempts >= t.max_retries else
                "retry_not_allowed_or_budget_exhausted"
            )
            # 队列已放弃重试时，在同一事务里关闭等待存档，防止恢复入口再次运行。
            if checkpoint is not None and checkpoint.status in {"active", "waiting_retry"}:
                checkpoint.status = terminal_status(outcome)
                checkpoint.result_json = json.dumps(outcome, ensure_ascii=False)
                checkpoint.updated_at = now
                run = s.query(AgentRun).filter_by(run_id=t.run_id).first()
                if run is not None:
                    run.status = "failed"
        else:
            t.status = "retrying"
            t.finished_at = None
            t.retry_after = now + timedelta(seconds=delay)
        if outcome is not None:
            t.result_json = json.dumps(outcome, ensure_ascii=False)
        t.lease_token = t.lease_until = None
        s.commit()


def settings_task_backoff(attempt: int) -> float:
    """指数退避：5s * 2^(attempt-1)。"""
    from app.config import settings

    return settings.TASK_RETRY_BACKOFF_SECONDS * (2 ** max(attempt - 1, 0))


def get_task(task_id: int) -> Task | None:
    with write_session() as s:
        return s.get(Task, task_id)


def count_pending() -> int:
    with write_session() as s:
        return s.query(Task).filter(Task.status.in_(["pending", "retrying"])).count()


def recover_stale_tasks(max_age_seconds: int | None = None, *, batch_size: int = 100) -> int:
    """回收失效租约；复用终态、受限重试或明确终止。返回本批实际处理数量。

    max_age_seconds 仅用于限制历史无租约记录的扫描年龄，不替代新任务的租约。
    升级要求旧 Worker 全停；默认扫描无租约 running，缺少预算则人工检查，禁止盲目重跑。
    """
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("回收批量必须为正整数")
    now = utcnow()
    legacy = Task.lease_token.is_(None)
    if max_age_seconds is not None:
        legacy = legacy & (Task.started_at < now - timedelta(seconds=max_age_seconds))
    expired = or_(Task.lease_until <= now, legacy,
                  Task.lease_token.isnot(None) & Task.lease_until.is_(None))
    with write_session() as s:
        stale = (
            s.query(Task)
            .filter(Task.status == "running", expired)
            .order_by(Task.id).limit(batch_size).with_for_update(skip_locked=True)
            .all()
        )
        recovered = 0
        for t in stale:
            # 与续租/结果写入竞争时以条件更新仲裁，不使用读取后的旧状态覆盖。
            matched = s.execute(update(Task).where(
                Task.id == t.id, Task.status == "running", Task.attempts == t.attempts,
                Task.lease_token == t.lease_token, expired,
            ).values(lease_token=None, lease_until=None).execution_options(synchronize_session=False))
            if matched.rowcount != 1:
                continue
            s.refresh(t)
            recovered += 1
            allowed, state = _resume_allowed(s, t, now)
            if not allowed:
                continue
            delay = settings_task_backoff(t.attempts)
            remaining = (t.deadline_at - now).total_seconds()
            if state is not None:
                delay = max(delay, state["retry_not_before"] - now.replace(tzinfo=timezone.utc).timestamp())
                budget = RunBudget.model_validate(state["budget"])
                remaining = min(remaining, min(budget.seconds_limit, settings.AGENT_TOTAL_TIMEOUT_SECONDS) - budget.elapsed_ms / 1000)
            if not math.isfinite(delay) or delay >= remaining:
                checkpoint = s.query(AgentCheckpoint).filter_by(run_id=t.run_id).first() if t.run_id else None
                _stop_recovery(s, t, checkpoint, now, "total_timeout", incomplete=True)
                continue
            t.status = "retrying"
            t.started_at = None
            t.finished_at = None
            t.retry_after = now + timedelta(seconds=delay)
            t.error = "lease_expired"
            run = s.query(AgentRun).filter_by(run_id=t.run_id).first() if t.run_id else None
            if run is not None:
                run.status = "retrying"
        s.commit()
        return recovered

"""DB 队列：任务创建、抢占、完成、失败重试（指数退避）。

幂等：idempotency_key 唯一索引，重复提交同一 key 返回已有任务。
抢占：SELECT ... FOR UPDATE SKIP LOCKED（MySQL 8 支持；SQLite 忽略该子句）。
优先级：priority 小的先执行；同优先级按创建时间。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import text

from app.db import write_session
from app.models import Task, utcnow


def create_task(
    task_type: str = "diagnose",
    payload: dict | None = None,
    anomaly_id: int | None = None,
    priority: int = 5,
    idempotency_key: str | None = None,
    max_retries: int | None = None,
) -> Task:
    """创建任务；同 idempotency_key 已存在则返回已有任务（幂等）。"""
    idem = idempotency_key or f"{task_type}:{json.dumps(payload or {}, ensure_ascii=False)}"
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
            max_retries=max_retries if max_retries is not None else 3,
        )
        s.add(task)
        s.commit()
        s.refresh(task)
        return task


def claim_pending(limit: int = 1) -> list[Task]:
    """抢占 pending 任务（含已到重试时间的 retrying）。"""
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
        for t in rows:
            t.status = "running"
            t.attempts += 1
            t.started_at = now
        s.commit()
        for t in rows:
            s.refresh(t)
        return rows


def complete_task(task_id: int, result: dict, run_id: str | None = None) -> None:
    with write_session() as s:
        t = s.get(Task, task_id)
        if t is None:
            return
        t.status = "succeeded"
        t.result_json = json.dumps(result, ensure_ascii=False)
        t.run_id = run_id
        t.finished_at = utcnow()
        s.commit()


def fail_task(task_id: int, error: str) -> None:
    """失败处理：未超重试次数则回队等待退避重试，否则置为 failed。"""
    with write_session() as s:
        t = s.get(Task, task_id)
        if t is None:
            return
        t.error = str(error)[:2000]
        if t.attempts >= t.max_retries:
            t.status = "failed"
            t.finished_at = utcnow()
        else:
            t.status = "retrying"
            t.retry_after = utcnow() + timedelta(seconds=settings_task_backoff(t.attempts))
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


def recover_stale_tasks(max_age_seconds: int = 1800) -> int:
    """回收卡死的 running 任务（Worker 崩溃/断电遗留），重置回 pending 重新排队。

    - 判定：status='running' 且 started_at 距今超过 max_age_seconds（默认 30 分钟）
    - 不增加 attempts：恢复不是业务失败，不消耗重试次数
    - 由调度器每 5 分钟调用 + Worker 启动时自愈调用，双保险
    """
    cutoff = utcnow() - timedelta(seconds=max_age_seconds)
    with write_session() as s:
        stale = (
            s.query(Task)
            .filter(
                Task.status == "running",
                Task.started_at.isnot(None),
                Task.started_at < cutoff,
            )
            .all()
        )
        for t in stale:
            t.status = "pending"
            t.started_at = None  # 清掉旧的启动时间，等待重新 claim
            t.error = (t.error + "; " if t.error else "") + "stale-recovered"
        s.commit()
        return len(stale)

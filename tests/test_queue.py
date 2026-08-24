"""任务队列测试：卡死任务回收（Worker 崩溃/断电自愈）。"""
from __future__ import annotations

import pytest

from app.db import write_session
from app.models import Task, utcnow
from app.tasks.queue import complete_task, recover_stale_tasks


def _clear_tasks():
    with write_session() as s:
        s.query(Task).delete()
        s.commit()


def _add_task(status: str, started_at=None) -> int:
    with write_session() as s:
        t = Task(
            task_type="diagnose",
            idempotency_key=f"t-{status}-{id(started_at)}",
            status=status,
            payload_json="{}",
            started_at=started_at,
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


def test_recover_stale_running_task():
    """running 且超时（30 分钟）的任务 → 重置为 pending，不消耗重试次数。"""
    _clear_tasks()
    from datetime import timedelta

    stale_id = _add_task("running", started_at=utcnow() - timedelta(hours=2))
    recovered = recover_stale_tasks()  # 默认 30 分钟阈值
    assert recovered == 1

    with write_session() as s:
        t = s.get(Task, stale_id)
        assert t.status == "pending"
        assert t.started_at is None
        assert t.attempts == 0  # 恢复不算业务失败，不消耗重试


def test_recover_skips_recent_running():
    """刚启动的 running 任务（未超时）不应被回收。"""
    _clear_tasks()
    recent_id = _add_task("running", started_at=utcnow())
    assert recover_stale_tasks() == 0
    with write_session() as s:
        assert s.get(Task, recent_id).status == "running"


def test_recover_skips_other_statuses():
    """pending / succeeded 任务不受影响。"""
    _clear_tasks()
    from datetime import timedelta

    old = utcnow() - timedelta(hours=2)
    _add_task("pending", started_at=None)
    _add_task("succeeded", started_at=old)
    _add_task("failed", started_at=old)
    assert recover_stale_tasks() == 0
    with write_session() as s:
        statuses = {t.status for t in s.query(Task).all()}
        assert statuses == {"pending", "succeeded", "failed"}


def test_recover_custom_threshold():
    """自定义阈值：1 分钟前启动的 running 任务在阈值 30 秒时被回收。"""
    _clear_tasks()
    from datetime import timedelta

    tid = _add_task("running", started_at=utcnow() - timedelta(seconds=90))
    assert recover_stale_tasks(max_age_seconds=30) == 1
    with write_session() as s:
        assert s.get(Task, tid).status == "pending"


def test_incomplete_agent_result_is_not_marked_succeeded():
    _clear_tasks()
    tid = _add_task("running", started_at=utcnow())
    complete_task(tid, {"status": "incomplete", "stop_reason": "token_budget"}, "run-x")
    with write_session() as s:
        task = s.get(Task, tid)
        assert task.status == "incomplete"
        assert task.run_id == "run-x"


def test_worker_survives_db_error(monkeypatch):
    """取任务时数据库报错，Worker 不崩溃：打印日志后继续轮询（自愈）。"""
    import asyncio

    from app.tasks import worker as w

    calls = {"n": 0}

    def fake_claim(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database connection lost")
        return []

    monkeypatch.setattr(w, "claim_pending", fake_claim)
    monkeypatch.setattr(w, "recover_stale_tasks", lambda: 0)  # 跳过启动自愈的 DB 访问
    monkeypatch.setattr(w.settings, "TASK_POLL_INTERVAL_SECONDS", 0.01)
    stop = asyncio.Event()

    async def _main():
        t = asyncio.create_task(w.run_worker(stop))
        await asyncio.sleep(0.08)   # 让循环跑几轮（第一轮抛异常）
        stop.set()
        await t

    asyncio.run(_main())
    assert calls["n"] >= 2  # 第一次抛异常后，循环继续轮询而不是退出

"""任务领取凭证。所有持久化路径先锁 Task，再写报告/Checkpoint，避免旧执行者覆盖。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from threading import Event

from sqlalchemy import update

from app.config import settings
from app.db import write_session
from app.models import Task, utcnow


class OwnershipLost(RuntimeError):
    """租约失效；禁止把旧执行者的结果或异常写回新任务。"""


@dataclass
class Ownership:
    task_id: int
    token: str
    attempt: int
    lost: Event = field(default_factory=Event)


current_owner: ContextVar[Ownership | None] = ContextVar("task_owner", default=None)


@contextmanager
def use_owner(owner: Ownership):
    marker = current_owner.set(owner)
    try:
        yield
    finally:
        current_owner.reset(marker)


def lock_owned_task(session, task_id: int, *, run_id: str | None = None) -> Task:
    owner = current_owner.get()
    if owner is None or owner.task_id != task_id or owner.lost.is_set():
        raise OwnershipLost("缺少有效的任务领取凭证")
    # UPDATE 条件在数据库内原子判断。SQLite 不支持 FOR UPDATE，也必须有写锁保护。
    conditions = [Task.id == task_id, Task.status == "running",
                  Task.attempts == owner.attempt, Task.lease_token == owner.token,
                  Task.lease_until > utcnow()]
    if run_id is not None:
        conditions.append(Task.run_id == run_id)
    matched = session.execute(update(Task).where(*conditions).values(
        lease_until=Task.lease_until).execution_options(synchronize_session=False))
    if matched.rowcount != 1:
        owner.lost.set()
        raise OwnershipLost("任务租约已失效或已由其他执行者接管")
    task = session.get(Task, task_id, populate_existing=True)
    # SQL 的 now 参数可能在等待行锁之前绑定；拿到锁后再确认一次真实当前时间。
    if task.lease_until <= utcnow() or owner.lost.is_set():
        owner.lost.set()
        raise OwnershipLost("等待数据库锁期间租约已失效")
    return task


def check_ownership(task_id: int | None, run_id: str | None = None) -> None:
    if task_id is not None:
        with write_session() as session:
            lock_owned_task(session, task_id, run_id=run_id)
            # 仅检查；离开会话释放锁，不延长租约。


def task_deadline(task_id: int, run_id: str):
    with write_session() as session:
        task = lock_owned_task(session, task_id, run_id=run_id)
        if task.deadline_at is None:
            raise ValueError("任务缺少原始截止时间，禁止重新建立预算")
        return task.deadline_at


def renew_lease() -> None:
    owner = current_owner.get()
    if owner is None:
        raise OwnershipLost("缺少领取凭证")
    with write_session() as session:
        task = lock_owned_task(session, owner.task_id)
        now = utcnow()
        if task.deadline_at is not None and task.deadline_at <= now:
            owner.lost.set()
            raise OwnershipLost("诊断原始截止时间已到，不再续租")
        task.heartbeat_at = now
        task.lease_until = now + timedelta(seconds=settings.TASK_LEASE_SECONDS)
        session.commit()

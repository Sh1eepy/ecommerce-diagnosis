"""任务队列测试：卡死任务回收（Worker 崩溃/断电自愈）。"""
from __future__ import annotations

import pytest

from app.db import write_session
from app.models import Task, utcnow
from app.tasks.queue import complete_task, ensure_task_run_id, recover_stale_tasks


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
    """历史无租约且无预算记录不能盲目回队。"""
    _clear_tasks()
    from datetime import timedelta

    stale_id = _add_task("running", started_at=utcnow() - timedelta(hours=2))
    recovered = recover_stale_tasks()
    assert recovered == 1

    with write_session() as s:
        t = s.get(Task, stale_id)
        assert t.status == "failed"
        assert t.error == "recovery_metadata_missing"
        assert t.attempts == 0


def test_recover_skips_recent_running():
    """刚启动的 running 任务（未超时）不应被回收。"""
    _clear_tasks()
    recent_id = _add_task("running", started_at=utcnow())
    from datetime import timedelta
    with write_session() as s:
        t = s.get(Task, recent_id)
        t.lease_token = "healthy"
        t.lease_until = utcnow() + timedelta(seconds=60)
        s.commit()
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
    """自定义年龄阈值只用于筛选旧记录，不会凭空补建预算。"""
    _clear_tasks()
    from datetime import timedelta

    tid = _add_task("running", started_at=utcnow() - timedelta(seconds=90))
    assert recover_stale_tasks(max_age_seconds=30) == 1
    with write_session() as s:
        assert s.get(Task, tid).status == "failed"


def test_incomplete_agent_result_is_not_marked_succeeded():
    _clear_tasks()
    tid = _add_task("running", started_at=utcnow())
    complete_task(tid, {"status": "incomplete", "stop_reason": "token_budget"}, "run-x")
    with write_session() as s:
        task = s.get(Task, tid)
        assert task.status == "incomplete"
        assert task.run_id == "run-x"


def test_task_run_id_is_stable_across_retries():
    _clear_tasks()
    tid = _add_task("running", started_at=utcnow())
    first = ensure_task_run_id(tid)
    second = ensure_task_run_id(tid)
    assert first == second
    with write_session() as s:
        assert s.get(Task, tid).run_id == first


def test_late_worker_cannot_overwrite_terminal_task():
    from app.tasks.queue import fail_task

    _clear_tasks()
    tid = _add_task("running", started_at=utcnow())
    complete_task(tid, {"status": "ok"}, "stable-run")
    complete_task(tid, {"status": "incomplete"}, "late-run")
    fail_task(tid, "late failure")
    with write_session() as s:
        task = s.get(Task, tid)
        assert task.status == "succeeded"
        assert task.run_id == "stable-run"
        assert task.error == ""


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


@pytest.mark.parametrize("cancel", [False, True])
def test_worker_capacity_and_shutdown_drain(monkeypatch, cancel):
    import asyncio
    from types import SimpleNamespace
    from app.tasks import worker as w

    async def scenario():
        stop = asyncio.Event()
        releases = [asyncio.Event() for _ in range(3)]
        started = [asyncio.Event() for _ in range(3)]
        claims = []
        next_id = 0

        def claim(limit):
            nonlocal next_id
            claims.append(limit)
            rows = [SimpleNamespace(id=i, attempts=1, lease_token="test") for i in range(next_id, min(3, next_id + limit))]
            next_id += len(rows)
            return rows

        async def process(task_id, sem, attempt, *args):
            started[task_id].set()
            await releases[task_id].wait()

        monkeypatch.setattr(w, "claim_pending", claim)
        monkeypatch.setattr(w, "_process", process)
        monkeypatch.setattr(w, "recover_stale_tasks", lambda: 0)
        monkeypatch.setattr(w.settings, "WORKER_CONCURRENCY", 2)
        monkeypatch.setattr(w.settings, "TASK_POLL_INTERVAL_SECONDS", .01)
        worker = asyncio.create_task(w.run_worker(stop))
        await asyncio.wait_for(started[1].wait(), 2)
        await asyncio.sleep(.04)
        assert claims == [2]  # 两个都忙时不能继续预领。
        releases[0].set()
        await asyncio.wait_for(started[2].wait(), 2)
        assert claims == [2, 1]
        if cancel:
            worker.cancel()
        else:
            stop.set()
        await asyncio.sleep(.03)
        assert not worker.done()  # 正在排空，而不是丢弃在途任务。
        releases[1].set()
        releases[2].set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 2)
        else:
            await asyncio.wait_for(worker, 2)
        assert claims == [2, 1]

    asyncio.run(scenario())


def test_stop_during_claim_does_not_abandon_claimed_tasks(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.tasks import worker as w

    async def scenario():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        processed = []

        def claim(limit):
            loop.call_soon_threadsafe(stop.set)
            return [SimpleNamespace(id=9, attempts=1, lease_token="test")]

        async def process(task_id, sem, attempt, *args):
            processed.append(task_id)

        monkeypatch.setattr(w, "claim_pending", claim)
        monkeypatch.setattr(w, "_process", process)
        monkeypatch.setattr(w, "recover_stale_tasks", lambda: 0)
        await asyncio.wait_for(w.run_worker(stop), 2)
        assert processed == [9]

    asyncio.run(scenario())


def test_worker_schema_migration_is_explicit_idempotent_and_preserves_data():
    from sqlalchemy import create_engine, text, inspect
    from app.task_schema import migration_statements, migrate_task_schema, require_task_schema

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE task (id INTEGER PRIMARY KEY, status VARCHAR(16), payload_json TEXT)"))
        connection.execute(text("INSERT INTO task VALUES (1, 'pending', 'original')"))
    assert len(migration_statements(engine)) == 4
    assert len(inspect(engine).get_columns("task")) == 3  # 预览不修改。
    with pytest.raises(RuntimeError, match="升级"):
        require_task_schema(engine)
    assert len(migrate_task_schema(engine)) == 4
    assert migrate_task_schema(engine) == []
    require_task_schema(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT status, payload_json, lease_token FROM task WHERE id=1")).one()
        assert tuple(row) == ("pending", "original", None)
    engine.dispose()


def test_concurrent_claims_do_not_share_a_task():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.tasks.queue import claim_pending, create_task

    _clear_tasks()
    task = create_task(idempotency_key="concurrent-claim")
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=2)
        return claim_pending(1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        claimed = [t for future in futures for t in future.result(timeout=5)]
    assert [t.id for t in claimed] == [task.id]
    assert claimed[0].attempts == 1 and claimed[0].lease_token


def test_worker_preflight_counts_without_mutating_or_reading_payloads():
    from datetime import timedelta
    from sqlalchemy import event
    from app.db import get_write_engine
    from scripts.worker_preflight import inspect_queue

    _clear_tasks()
    now = utcnow()
    rows = [
        dict(status="pending"),
        dict(status="pending", retry_after=now + timedelta(seconds=10)),
        dict(status="retrying", attempts=1, retry_after=now,
             deadline_at=now - timedelta(seconds=1)),
        dict(status="retrying", attempts=3, retry_after=now + timedelta(seconds=10)),
        dict(status="running", attempts=1, lease_token="private-token", lease_until=now + timedelta(seconds=10),
             deadline_at=now + timedelta(seconds=30)),
        dict(status="running", attempts=1, lease_token="expired-token", lease_until=now),
        dict(status="running", attempts=1),
        dict(status="succeeded"), dict(status="failed"), dict(status="error"), dict(status="incomplete"),
        dict(status="unexpected"),
    ]
    with write_session() as session:
        for index, fields in enumerate(rows):
            session.add(Task(idempotency_key=f"preflight-{index}", payload_json="private payload",
                             result_json="private result", **fields))
        session.commit()
    engine = get_write_engine()
    statements = []

    def record(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        with engine.connect() as connection:
            counts = inspect_queue(connection, now)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert counts == dict(total=12, pending=2, retrying=2, running=3, succeeded=1,
                          incomplete=1, failed=2, unknown=1, due_queue_candidates=2,
                          recoverable_running_candidates=2, attempts_exhausted=1,
                          deadline_expired=1, missing_previous_deadline=3, unfinished=7)
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
    assert all(name not in statements[0] for name in ("payload_json", "result_json", "heartbeat_at"))
    with write_session() as session:
        after = session.query(Task).order_by(Task.id).all()
        assert [task.status for task in after] == [fields["status"] for fields in rows]
        assert after[4].lease_token == "private-token"


@pytest.mark.parametrize("api_key, mode", [("secret-model-key", "external_api"), ("", "mock")])
def test_worker_preflight_empty_queue_and_secret_safe_report(monkeypatch, capsys, api_key, mode):
    from app.config import settings
    from app import llm
    from scripts.worker_preflight import main

    _clear_tasks()
    monkeypatch.setattr(settings, "LLM_API_KEY", api_key)
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://private-webhook")
    monkeypatch.setattr(llm, "get_llm", lambda: pytest.fail("preflight must not construct a model"))
    assert main() == 0
    output = capsys.readouterr().out
    assert '"total": 0' in output and '"unfinished": 0' in output
    assert f'"model_mode": "{mode}"' in output
    assert '"alert_configured": true' in output
    assert "secret-model-key" not in output and "private-webhook" not in output


def test_worker_preflight_redacts_database_errors(monkeypatch, capsys):
    from app import db
    from scripts.worker_preflight import main

    def unavailable():
        raise RuntimeError("mysql://private-user:private-password@host/db")

    monkeypatch.setattr(db, "get_write_engine", unavailable)
    assert main() == 1
    output = capsys.readouterr()
    assert "RuntimeError" in output.err
    assert "private-password" not in output.err + output.out

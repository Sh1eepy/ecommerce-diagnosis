"""asyncio Worker：轮询 DB 队列 → 并发执行 Agent → 更新任务状态。

按空闲槽位领取，独立诊断线程池；控制面维护租约/心跳与受限恢复。
正常停止先停止领取，再排空在途任务。强杀与断电依赖持久化恢复。
"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import date

from app.agent.agent import Agent
from app.agent.state import FailureInfo
from app.config import settings
from app.models import Task
from app.task_ownership import Ownership, OwnershipLost, check_ownership, renew_lease, use_owner
from app.tasks.queue import (
    claim_pending,
    complete_task,
    ensure_task_run_id,
    fail_task,
    recover_stale_tasks,
)


def _load_task(task_id: int) -> Task | None:
    from app.tasks.queue import get_task

    return get_task(task_id)


def _run_diagnose(task: Task) -> dict:
    payload = json.loads(task.payload_json or "{}")
    start = date.fromisoformat(payload["start_date"])
    end = date.fromisoformat(payload["end_date"])
    item_id = int(payload["item_id"])

    # 类目级异常（item_id=0 + category_id）：取该类目窗口内 UV 最高的商品作锚点，
    # 用锚点商品数据回答"整个类目为什么崩"（Agent 工具均为商品级）。
    if item_id == 0 and payload.get("category_id"):
        from app.detection.detector import find_anchor_item

        anchor = find_anchor_item(int(payload["category_id"]), start, end)
        if anchor is None:
            raise ValueError(f"类目 {payload['category_id']} 无可用锚点商品（无该类目切片数据）")
        payload["anchor_item_id"] = anchor  # 记录锚点，审计可回溯
        item_id = anchor

    stable_run_id = ensure_task_run_id(task.id)
    agent = Agent()
    return agent.run(
        item_id=item_id,
        start=start,
        end=end,
        anomaly=payload.get("anomaly", ""),
        anomaly_id=task.anomaly_id,
        run_id=stable_run_id,
        task_id=task.id,
    )


async def _process(task_id: int, sem: asyncio.Semaphore, expected_attempt: int | None = None,
                   expected_token: str | None = None, executor=None) -> None:
    async with sem:
        # 读取失败时没有已验证的凭证，不能尝试把当前任务写成失败。
        try:
            task = await asyncio.to_thread(_load_task, task_id)
        except Exception as error:
            print(f"[worker] 读取任务失败: {type(error).__name__}", flush=True)
            return
        if (task is None or task.status != "running" or not task.lease_token
                or (expected_attempt is not None and task.attempts != expected_attempt)
                or (expected_token is not None and task.lease_token != expected_token)):
            return
        owner = Ownership(task_id, task.lease_token, task.attempts)
        done = asyncio.Event()

        def renew_once():
            try:
                renew_lease()
            except Exception:
                owner.lost.set()  # 在控制线程内立即标记，不等待事件循环再次获得执行权。
                raise

        async def heartbeat():
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), settings.TASK_HEARTBEAT_SECONDS)
                except TimeoutError:
                    try:
                        await asyncio.to_thread(renew_once)
                    except Exception as error:
                        # 续租失败不证明进程已死，但不能继续以有效所有者自居。
                        owner.lost.set()
                        print(f"[worker] 续租停止: {type(error).__name__}", flush=True)
                        return

        with use_owner(owner):
            heartbeat_job = asyncio.create_task(heartbeat())
            try:
                await asyncio.to_thread(check_ownership, task_id)
                if task.task_type != "diagnose":
                    raise ValueError("未知任务类型")
                if executor is None:  # 单条任务测试入口。
                    result = await asyncio.to_thread(_run_diagnose, task)
                else:
                    result = await asyncio.get_running_loop().run_in_executor(
                        executor, copy_context().run, _run_diagnose, task)
                await asyncio.to_thread(check_ownership, task_id)
                if not isinstance(result, dict):
                    raise ValueError("无效 Agent 结果")
                if result.get("status") == "error":
                    try:
                        failure = FailureInfo.model_validate(result.get("failure"))
                    except (TypeError, ValueError):
                        failure = FailureInfo(kind="unknown", reason="invalid_failure_metadata")
                    await asyncio.to_thread(
                        fail_task, task_id, failure.summary(),
                        retryable=failure.retryable and failure.kind == "retryable",
                        retry_after_seconds=failure.retry_after_seconds, result=result,
                        expected_attempt=task.attempts,
                    )
                else:
                    await asyncio.to_thread(complete_task, task_id, result, result.get("run_id"),
                                            expected_attempt=task.attempts)
            except OwnershipLost:
                pass  # 旧执行者既不能提交结果，也不能把新执行者标记失败。
            except Exception as error:
                if not owner.lost.is_set():
                    await asyncio.to_thread(fail_task, task_id, f"Worker failure: {type(error).__name__}",
                                            expected_attempt=task.attempts)
            finally:
                done.set()
                await heartbeat_job


async def run_worker(stop_event: asyncio.Event | None = None) -> None:
    """主循环：持续抢占任务并调度执行。"""
    # 启动恢复：复用终态、按预算回队，或明确结束失效任务。
    try:
        n = await asyncio.to_thread(recover_stale_tasks)
        if n:
            print(f"[worker] 启动时回收卡死任务 {n} 个", flush=True)
    except Exception:  # noqa: BLE001  回收失败不阻塞启动
        pass
    stop_event = stop_event or asyncio.Event()
    sem = asyncio.Semaphore(settings.WORKER_CONCURRENCY)
    # 长时间 Agent 调用和控制面数据库操作分池，避免所有槽位忙时饿死心跳。
    agent_pool = ThreadPoolExecutor(max_workers=settings.WORKER_CONCURRENCY, thread_name_prefix="agent")
    in_flight: set[asyncio.Task] = set()

    def finished(job: asyncio.Task) -> None:
        in_flight.discard(job)
        if not job.cancelled() and job.exception() is not None:
            print(f"[worker] 任务处理异常: {type(job.exception()).__name__}", flush=True)

    async def dispatch() -> None:
        last_recovery = asyncio.get_running_loop().time()
        while not stop_event.is_set():
            now = asyncio.get_running_loop().time()
            if now - last_recovery >= settings.TASK_RECOVERY_INTERVAL_SECONDS:
                try:
                    await asyncio.to_thread(recover_stale_tasks)
                except Exception as error:
                    print(f"[worker] 回收检查失败: {type(error).__name__}", flush=True)
                last_recovery = now
            slots = settings.WORKER_CONCURRENCY - len(in_flight)
            if slots > 0:
                try:
                    tasks = await asyncio.to_thread(claim_pending, slots)
                except Exception as error:  # 不输出可能含连接凭证的异常正文。
                    print(f"[worker] 取任务失败: {type(error).__name__}", flush=True)
                else:
                    # 停机信号可能在数据库领取期间到达；已经领取的任务仍须处理。
                    for task in tasks:
                        job = asyncio.create_task(_process(task.id, sem, task.attempts,
                                                           task.lease_token, agent_pool))
                        in_flight.add(job)
                        job.add_done_callback(finished)
            try:
                await asyncio.wait_for(stop_event.wait(), settings.TASK_POLL_INTERVAL_SECONDS)
            except TimeoutError:
                pass

    dispatcher = asyncio.create_task(dispatch())
    try:
        # 取消调度协程不会停止正在 to_thread 中执行的同步代码。
        await asyncio.shield(dispatcher)
    except asyncio.CancelledError:
        stop_event.set()
        await dispatcher
        raise
    finally:
        stop_event.set()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
        agent_pool.shutdown(wait=True)

"""asyncio Worker：轮询 DB 队列 → 并发执行 Agent → 更新任务状态。

上线特性（V3~V4）：
- 并发限制：asyncio.Semaphore（WORKER_CONCURRENCY）
- 失败重试：指数退避（queue.fail_task 回队 + retry_after）
- 任务级处理：每次抢占一个任务独立执行，互不阻塞
- 幂等：创建端靠 idempotency_key 唯一约束
- 后续工程化：队列换 Redis/Celery，Worker 多副本（本版不实现）
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

from app.agent.agent import Agent
from app.config import settings
from app.models import Task
from app.tasks.queue import claim_pending, complete_task, fail_task, recover_stale_tasks


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

    agent = Agent()
    return agent.run(
        item_id=item_id,
        start=start,
        end=end,
        anomaly=payload.get("anomaly", ""),
        anomaly_id=task.anomaly_id,
    )


async def _process(task_id: int, sem: asyncio.Semaphore) -> None:
    async with sem:
        try:
            task = await asyncio.to_thread(_load_task, task_id)
            if task is None:
                return
            if task.status != "running":
                return  # 已被其他 Worker 处理
            if task.task_type == "diagnose":
                result = await asyncio.to_thread(_run_diagnose, task)
            else:
                result = {"error": f"未知任务类型: {task.task_type}"}
            await asyncio.to_thread(complete_task, task_id, result, result.get("run_id"))
        except Exception as e:  # noqa: BLE001
            await asyncio.to_thread(fail_task, task_id, str(e))


async def run_worker(stop_event: asyncio.Event | None = None) -> None:
    """主循环：持续抢占任务并调度执行。"""
    # 启动自愈：回收上次进程崩溃/断电遗留的卡死任务（重置回队列重新消费）
    try:
        n = await asyncio.to_thread(recover_stale_tasks)
        if n:
            print(f"[worker] 启动时回收卡死任务 {n} 个", flush=True)
    except Exception:  # noqa: BLE001  回收失败不阻塞启动
        pass
    sem = asyncio.Semaphore(settings.WORKER_CONCURRENCY)
    while stop_event is None or not stop_event.is_set():
        # 取任务走数据库：DB 断连/网络抖动时不能把 Worker 整个带崩，
        # 打印日志 + 短暂退避后重试，让 Worker 自己活过来（配合调度器的卡死回收兜底）。
        try:
            tasks = await asyncio.to_thread(claim_pending, settings.WORKER_CONCURRENCY)
        except Exception as e:  # noqa: BLE001
            print(f"[worker] 取任务失败（数据库连接问题？）: {type(e).__name__}: {e}，"
                  f"{settings.TASK_POLL_INTERVAL_SECONDS}s 后重试", flush=True)
            await asyncio.sleep(settings.TASK_POLL_INTERVAL_SECONDS)
            continue
        if not tasks:
            await asyncio.sleep(settings.TASK_POLL_INTERVAL_SECONDS)
            continue
        for t in tasks:
            asyncio.create_task(_process(t.id, sem))
        await asyncio.sleep(0.05)

"""任务查询接口。"""
from __future__ import annotations

import json
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException

from app.security import require_scope
from app.tasks.queue import get_task

router = APIRouter(tags=["tasks"])


def _utc_iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value is not None else None


@router.get("/tasks/{task_id}")
def task_status(task_id: int, _: str = Depends(require_scope("report:read"))) -> dict:
    t = get_task(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "task_id": t.id,
        "task_type": t.task_type,
        "status": t.status,
        "attempts": t.attempts,
        "max_attempts": t.max_retries,
        "retry_after": _utc_iso(t.retry_after),
        "deadline_at": _utc_iso(t.deadline_at),
        "heartbeat_at": _utc_iso(t.heartbeat_at),
        "lease_until": _utc_iso(t.lease_until),
        "started_at": _utc_iso(t.started_at),
        "finished_at": _utc_iso(t.finished_at),
        "priority": t.priority,
        "run_id": t.run_id,
        "anomaly_id": t.anomaly_id,
        "error": t.error,
        "result": json.loads(t.result_json) if t.result_json else None,
        "created_at": str(t.created_at),
    }
